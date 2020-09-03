import torch
import numpy as np
import itertools
from collections import defaultdict

from mlreco.models.uresnet_lonely import UResNet, SegmentationLoss
from mlreco.models.ppn import PPN, PPNLoss
from mlreco.models.layers.dbscan import DBSCANFragmenter
from .gnn import node_encoder_construct, edge_encoder_construct
from mlreco.models.gnn.modular_meta import MetaLayerModel as GNN
from mlreco.models.cluster_full_gnn import ChainLoss as FullGNNLoss
from mlreco.models.cluster_gnn import EdgeChannelLoss as EdgeGNNLoss

from mlreco.utils.gnn.evaluation import node_assignment_score, primary_assignment
from mlreco.utils.gnn.network import complete_graph
from mlreco.utils.gnn.cluster import cluster_direction, get_cluster_batch

class FullChain(torch.nn.Module):
    """
    Driver class for the end-to-end reconstruction chain
    1) UResNet
        1) Semantic - for point classification
        2) PPN - for particle point locations
        3) Fragment - to form particle fragments
    2) GNN
        1) Particle - to group showers and identify their primaries
        2) Interaction - to group particles

    For use in config:
    model:
      name: full_chain
      modules:
        uresnet_lonely:
          <UResNet parameters, see mlreco/models/uresnet_lonely.py>
        ppn:
          <PPN parameters, see mlreco/models/ppn.py>
        dbscan_frag:
          <DBSCAN fragmenter parameters, see mlreco/models/layers/dbscan.py>
        node_encoder:
          <Node encoder parameters, see mlreco/models/gnn/factories.py>
        edge_encoder:
          <Edge encoder parameters, see mlreco/models/gnn/factories.py>
        particle_gnn:
          node_type    : <fragment semantic class to include in the particle grouping task>
        interaction_gnn:
          node_type    : <particle semantic class to include in the interaction grouping task>
        particle_edge_model:
          <GNN parameters for particle clustering, see mlreco/models/gnn/modular_nnconv.py>
        interaction_edge_model:
          <GNN parameters for interaction clustering, see mlreco/models/gnn/modular_nnconv.py>
        full_chain_loss:
          segmentation_weight: <relative weight of the segmentation loss>
          ppn_weight: <relative weight of the ppn loss>
          particle_gnn_weight: <relative weight of the particle gnn loss>
          interaction_gnn_weight: <relative weight of the interaction gnn loss>
    """

    MODULES = ['full_cnn', 'particle_gnn', 'interaction_gnn', 'full_chain_loss', 'uresnet_lonely',
               'particle_edge_model', 'interaction_edge_model', 'ppn', 'node_encoder', 'edge_encoder', 'dbscan_frag']

    def __init__(self, cfg, name='full_chain'):
        super(FullChain, self).__init__()

        # Initialize the UResNet+PPN modules
        self.uresnet_lonely = UResNet(cfg)
        self.ppn            = PPN(cfg)

        # Initialize the DBSCAN fragmenter
        self.dbscan_frag = DBSCANFragmenter(cfg)

        # Initialize the geometric encoders
        self.node_encoder = node_encoder_construct(cfg)
        self.edge_encoder = edge_encoder_construct(cfg)

        # Initialize the GNN models
        self.particle_gnn  = GNN(cfg['particle_edge_model'])
        self.inter_gnn     = GNN(cfg['interaction_edge_model'])

    def forward(self, input):
        '''
        Forward for full reconstruction chain.

        INPUTS:
            - input (N x 5 Tensor): Input data [x, y, z, batch_id, val]

        RETURNS:
            - result (tuple of dicts): (cnn_result, gnn_result)
        '''
        # Pass the input data through UResNet+PPN (semantic segmentation + point prediction)
        device = input[0].device
        result = self.uresnet_lonely([input[0][:,:5]])
        ppn_input = {}
        ppn_input.update(result)
        ppn_input['ppn_feature_enc'] = ppn_input['ppn_feature_enc'][0]
        ppn_input['ppn_feature_dec'] = ppn_input['ppn_feature_dec'][0]
        if 'ghost' in ppn_input:
            ppn_input['ghost'] = ppn_input['ghost'][0]
        ppn_output = self.ppn(ppn_input)
        result.update(ppn_output)

        # Get the fragment predictions from the DBSCAN fragmenter
        semantic_labels = torch.argmax(result['segmentation'][0], dim=1).flatten().double()
        semantic_data = torch.cat((input[0][:,:4], semantic_labels.reshape(-1,1)), dim=1)
        fragments = self.dbscan_frag(semantic_data, result)
        frag_batch_ids = get_cluster_batch(input[0], fragments)
        frag_seg = np.empty(len(fragments), dtype=np.int32)
        for i, f in enumerate(fragments):
            vals, cnts = semantic_labels[f].unique(return_counts=True)
            assert len(vals) == 1
            frag_seg[i] = vals[torch.argmax(cnts)].item()

        # Initialize a complete graph for edge prediction, get shower fragment and edge features
        em_mask = np.where(frag_seg == 0)[0]
        edge_index = complete_graph(frag_batch_ids[em_mask])
        x = self.node_encoder(input[0], fragments[em_mask])
        e = self.edge_encoder(input[0], fragments[em_mask], edge_index)

        # Extract shower starts from PPN predictions (most likely prediction)
        ppn_points = result['points'][0].detach()
        ppn_feats = torch.empty((0,6), device=device, dtype=torch.float)
        for f in fragments[em_mask]:
            scores = torch.softmax(ppn_points[f,3:5], dim=1)
            argmax = torch.argmax(scores[:,-1])
            start  = input[0][f][argmax,:3].float()+ppn_points[f][argmax,:3]+0.5
            dir = cluster_direction(input[0][f][:,:3].float(), start, max_dist=5)
            ppn_feats = torch.cat((ppn_feats, torch.cat([start, dir]).reshape(1,-1)), dim=0)

        x = torch.cat([x, ppn_feats], dim=1)

        # Pass shower fragment features through GNN
        index = torch.tensor(edge_index, dtype=torch.long, device=device)
        xbatch = torch.tensor(frag_batch_ids[em_mask], dtype=torch.long, device=device)
        gnn_output = self.particle_gnn(x, index, e, xbatch)

        # Divide the particle GNN output out into different arrays (one per batch)
        _, counts = torch.unique(input[0][:,3], return_counts=True)
        vids = np.concatenate([np.arange(n.item()) for n in counts])
        cids = np.concatenate([np.arange(n) for n in np.unique(frag_batch_ids[em_mask], return_counts=True)[1]])
        bcids = [np.where(frag_batch_ids[em_mask] == b)[0] for b in range(len(counts))]
        beids = [np.where(frag_batch_ids[em_mask][edge_index[0]] == b)[0] for b in range(len(counts))]

        node_pred = [gnn_output['node_pred'][0][b] for b in bcids]
        edge_pred = [gnn_output['edge_pred'][0][b] for b in beids]
        edge_index = [cids[edge_index[:,b]].T for b in beids]
        frags = [np.array([vids[c] for c in fragments[em_mask][b]]) for b in bcids]

        result.update({
            'fragments': [frags],
            'frag_node_pred': [node_pred],
            'frag_edge_pred': [edge_pred],
            'frag_edge_index': [edge_index]
        })

        # Make shower group predictions based on the GNN output, use truth during training
        group_ids = []
        for b in range(len(counts)):
            if not len(frags[b]):
                group_ids.append(np.array([], dtype = np.int64))
            else:
                group_ids.append(node_assignment_score(edge_index[b], edge_pred[b].detach().cpu().numpy(), len(frags[b])))

        result.update({'frag_group_pred': [group_ids]})

        # Merge fragments into particle instances, retain primary fragment id of showers
        particles, part_primary_ids = [], []
        for b in range(len(counts)):
            # Append one particle per shower group
            voxel_inds = counts[:b].sum().item()+np.arange(counts[b].item())
            primary_labels = primary_assignment(node_pred[b].detach().cpu().numpy(), group_ids[b])
            for g in np.unique(group_ids[b]):
                group_mask = np.where(group_ids[b] == g)[0]
                particles.append(voxel_inds[np.concatenate(frags[b][group_mask])])
                primary_id = group_mask[primary_labels[group_mask]][0]
                part_primary_ids.append(primary_id)

            # Append non-shower fragments as is
            mask = (frag_batch_ids == b) & (frag_seg != 0)
            particles.extend(fragments[mask])
            part_primary_ids.extend(-np.ones(np.sum(mask)))

        particles = np.array(particles)
        part_batch_ids = get_cluster_batch(input[0], particles)
        part_primary_ids = np.array(part_primary_ids, dtype=np.int32)
        part_seg = np.empty(len(particles), dtype=np.int32)
        for i, p in enumerate(particles):
            vals, cnts = semantic_labels[p].unique(return_counts=True)
            assert len(vals) == 1
            part_seg[i] = vals[torch.argmax(cnts)].item()

        # Initialize a complete graph for edge prediction, get particle and edge features
        edge_index = complete_graph(part_batch_ids)
        x = self.node_encoder(input[0], particles)
        e = self.edge_encoder(input[0], particles, edge_index)

        # Extract interesting points for particles, add semantic class, mean value and rms value
        # - For showers, take the most likely PPN voxel of the primary fragment
        # - For tracks, take the points furthest removed from each other
        # - For Michel and Delta, take the most likely PPN voxel
        ppn_feats = torch.empty((0,12), device=input[0].device, dtype=torch.float)
        for i, p in enumerate(particles):
            if part_seg[i] == 1:
                from mlreco.utils import local_cdist
                dist_mat = local_cdist(input[0][p,:3], input[0][p,:3])
                idx = torch.argmax(dist_mat)
                start_id, end_id = int(idx/len(p)), int(idx%len(p))
                start, end = input[0][p[start_id],:3].float(), input[0][p[end_id],:3].float()
                dir = end-start
                if dir.norm():
                    dir = dir/dir.norm()
            else:
                if part_seg[i] == 0:
                    voxel_inds = counts[:part_batch_ids[i]].sum().item()+np.arange(counts[part_batch_ids[i]].item())
                    p = voxel_inds[frags[part_batch_ids[i]][part_primary_ids[i]]]
                scores = torch.softmax(ppn_points[p,3:5], dim=1)
                argmax = torch.argmax(scores[:,-1])
                start = end = input[0][p][argmax,:3].float()+ppn_points[p][argmax,:3]+0.5
                dir = cluster_direction(input[0][p][:,:3].float(), start, max_dist=5)

            sem_type = torch.tensor([part_seg[i]], dtype=torch.float, device=device)
            values = torch.cat((input[0][p,4].mean().reshape(1), input[0][p,4].std().reshape(1))).float()
            ppn_feats = torch.cat((ppn_feats, torch.cat([values, sem_type.reshape(1), start, end, dir]).reshape(1,-1)), dim=0)

        x = torch.cat([x, ppn_feats], dim=1)

        # Pass particles through interaction clustering
        index = torch.tensor(edge_index, dtype=torch.long, device=device)
        xbatch = torch.tensor(part_batch_ids, dtype=torch.long, device=device)
        gnn_output = self.inter_gnn(x, index, e, xbatch)

        # Divide the interaction GNN output out into different arrays (one per batch)
        cids = np.concatenate([np.arange(n) for n in np.unique(part_batch_ids, return_counts=True)[1]])
        bcids = [np.where(part_batch_ids == b)[0] for b in range(len(counts))]
        beids = [np.where(part_batch_ids[edge_index[0]] == b)[0] for b in range(len(counts))]

        edge_pred = [gnn_output['edge_pred'][0][b] for b in beids]
        edge_index = [cids[edge_index[:,b]].T for b in beids]
        particles = [np.array([vids[c] for c in particles[b]]) for b in bcids]

        result.update({
            'particles': [particles],
            'inter_edge_pred': [edge_pred],
            'inter_edge_index': [edge_index]
        })

        return result


class FullChainLoss(torch.nn.modules.loss._Loss):
    def __init__(self, cfg):
        super(FullChainLoss, self).__init__()

        # Initialize loss components
        self.segmentation_loss = SegmentationLoss(cfg)
        self.ppn_loss = PPNLoss(cfg)
        self.particle_gnn_loss = FullGNNLoss(cfg, 'particle_gnn')
        self.inter_gnn_loss  = EdgeGNNLoss(cfg, 'interaction_gnn')

        # Initialize the loss weights
        self.loss_config = cfg['full_chain_loss']
        self.segmentation_weight = self.loss_config.get('segmentation_weight', 1.0)
        self.ppn_weight = self.loss_config.get('ppn_weight', 1.0)
        self.particle_gnn_weight = self.loss_config.get('particle_gnn_weight', 1.0)
        self.inter_gnn_weight = self.loss_config.get('particle_gnn_weight', 1.0)

    def forward(self, out, cluster_label, ppn_label):
        '''
        Forward propagation for FullChain

        INPUTS:
            - out (dict): result from forwarding three-tailed UResNet, with
            1) segmenation decoder 2) clustering decoder 3) seediness decoder,
            and PPN attachment to the segmentation branch.

            - cluster_label (list of Tensors): input data tensor of shape N x 10
              In row-index order:
              1. x coordinates
              2. y coordinates
              3. z coordinates
              4. batch indices
              5. energy depositions
              6. fragment labels
              7. group labels
              8. interaction labels
              9. neutrino labels
              10. segmentation labels (0-5, includes ghosts)

            - ppn_label (list of Tensors): particle labels for ppn ground truth
        '''

        # Apply the segmenation loss
        coords = cluster_label[0][:, :4]
        segment_label = cluster_label[0][:, -1]
        segment_label_tensor = torch.cat((coords, segment_label.reshape(-1,1)), dim=1)
        res_seg = self.segmentation_loss(out, [segment_label_tensor])
        seg_acc, seg_loss = res_seg['accuracy'], res_seg['loss']

        # Apply the PPN loss
        res_ppn = self.ppn_loss(out, [segment_label_tensor], ppn_label)

        # Apply the GNN particle clustering loss
        gnn_out = {
            'clusts':out['fragments'],
            'node_pred':out['frag_node_pred'],
            'edge_pred':out['frag_edge_pred'],
            'group_pred':out['frag_group_pred'],
            'edge_index':out['frag_edge_index'],
        }
        res_gnn_part = self.particle_gnn_loss(gnn_out, cluster_label)

        # Apply the GNN interaction grouping loss
        gnn_out = {
            'clusts':out['particles'],
            'edge_pred':out['inter_edge_pred'],
            'edge_index':out['inter_edge_index']
        }
        res_gnn_inter = self.inter_gnn_loss(gnn_out, cluster_label, None)

        # Combine the results
        accuracy = (res_seg['accuracy'] + res_ppn['ppn_acc'] \
                    + res_gnn_part['accuracy'] + res_gnn_inter['accuracy'])/4.
        loss = self.segmentation_weight*res_seg['loss'] \
             + self.ppn_weight*res_ppn['ppn_loss'] \
             + self.particle_gnn_weight*res_gnn_part['loss'] \
             + self.inter_gnn_weight*res_gnn_inter['loss']

        res = {}
        res.update(res_seg)
        res.update(res_ppn)
        res['seg_accuracy'] = seg_acc
        res['seg_loss'] = seg_loss
        res['ppn_accuracy'] = res_ppn['ppn_acc']
        res['ppn_loss'] = res_ppn['ppn_loss']
        res['frag_edge_loss'] = res_gnn_part['edge_loss']
        res['frag_node_loss'] = res_gnn_part['node_loss']
        res['frag_edge_accuracy'] = res_gnn_part['edge_accuracy']
        res['frag_node_accuracy'] = res_gnn_part['node_accuracy']
        res['inter_edge_loss'] = res_gnn_inter['loss']
        res['inter_edge_accuracy'] = res_gnn_inter['accuracy']
        res['loss'] = loss
        res['accuracy'] = accuracy

        print('Segmentation Accuracy: {:.4f}'.format(res_seg['accuracy']))
        print('PPN Accuracy: {:.4f}'.format(res_ppn['ppn_acc']))
        print('Shower fragment clustering accuracy: {:.4f}'.format(res_gnn_part['edge_accuracy']))
        print('Shower primary prediction accuracy: {:.4f}'.format(res_gnn_part['node_accuracy']))
        print('Interaction grouping accuracy: {:.4f}'.format(res_gnn_inter['accuracy']))

        return res
