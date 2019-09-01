# GNN on clusters.  No primaries

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
import torch
import numpy as np
from torch.nn import Sequential as Seq, Linear as Lin, ReLU, Sigmoid, LeakyReLU, Dropout, BatchNorm1d
from torch_geometric.nn import MetaLayer, GATConv
from mlreco.utils.gnn.cluster import get_cluster_batch, get_cluster_label, form_clusters_new
from mlreco.utils.gnn.primary import assign_primaries, analyze_primaries
from mlreco.utils.gnn.network import complete_graph
from mlreco.utils.gnn.compton import filter_compton
from mlreco.utils.gnn.data import cluster_vtx_features, cluster_edge_features, edge_assignment, cluster_vtx_features_old
from mlreco.utils.gnn.evaluation import secondary_matching_vox_efficiency, secondary_matching_vox_efficiency3
from mlreco.utils.gnn.evaluation import DBSCAN_cluster_metrics2, assign_clusters_UF
from mlreco.utils.groups import process_group_data
from .gnn import edge_model_construct

class EdgeModel(torch.nn.Module):
    """
    Driver for edge prediction, assumed to be with PyTorch GNN model.
    This class mostly acts as a wrapper that will hand the graph data to another model
    
    for use in config
    model:
        modules:
            edge_model:
                name: <name of edge model>
                model_cfg:
                    <dictionary of arguments to pass to model>
                remove_compton: <True/False to remove compton clusters> (default True)
    """
    def __init__(self, cfg):
        super(EdgeModel, self).__init__()
        
        if 'modules' in cfg:
            self.model_config = cfg['modules']['clust_edge_model']
        else:
            self.model_config = cfg
        
        self.remove_compton = self.model_config.get('remove_compton', True)
        self.compton_thresh = self.model_config.get('compton_thresh', 30)
            
        # extract the model to use
        model = edge_model_construct(self.model_config.get('name', 'edge_only'))
                     
        # construct the model
        self.edge_predictor = model(self.model_config.get('model_cfg', {}))
        
        
    def forward(self, data):
        """
        inputs data:
            data[0] - dbscan data
        output:
        dictionary, with
            'edge_pred': torch.tensor with edge prediction weights
        """
        # get device
        device = data[0].device
        
        # need to form graph, then pass through GNN
        clusts = form_clusters_new(data[0])
        
        # remove compton clusters
        # if no cluster fits this condition, return
        if self.remove_compton:
            selection = filter_compton(clusts, self.compton_thresh) # non-compton looking clusters
            if not len(selection):
                e = torch.tensor([], requires_grad=True)
                e.to(device)
                return {'edge_pred':[e]}

            clusts = clusts[selection]
        
        # form graph
        batch = get_cluster_batch(data[0], clusts)
        edge_index = complete_graph(batch, device=device)
        
        if not edge_index.shape[0]:
            e = torch.tensor([], requires_grad=True)
            e.to(device)
            return {'edge_pred':[e]}

        # obtain vertex features
        x = cluster_vtx_features(data[0], clusts, device=device)
        # obtain edge features
        e = cluster_edge_features(data[0], clusts, edge_index, device=device)
        # get x batch
        xbatch = torch.tensor(batch).to(device)
        
        # get output
        out = self.edge_predictor(x, edge_index, e, xbatch)
        
        return out
    
    
    
class EdgeChannelLoss(torch.nn.Module):
    """
    Edge loss based on two channel output
    """
    def __init__(self, cfg):
        # torch.nn.MSELoss(reduction='sum')
        # torch.nn.L1Loss(reduction='sum')
        super(EdgeChannelLoss, self).__init__()
        self.model_config = cfg['modules']['clust_edge_model']
        
        self.remove_compton = self.model_config.get('remove_compton', True)
        self.compton_thresh = self.model_config.get('compton_thresh', 30)
        
        self.reduction = self.model_config.get('reduction', 'mean')
        self.loss = self.model_config.get('loss', 'CE')
        
        if self.loss == 'CE':
            self.lossfn = torch.nn.CrossEntropyLoss(reduction=self.reduction)
        elif self.loss == 'MM':
            p = self.model_config.get('p', 1)
            margin = self.model_config.get('margin', 1.0)
            self.lossfn = torch.nn.MultiMarginLoss(p=p, margin=margin, reduction=self.reduction)
        else:
            raise Exception('unrecognized loss: ' + self.loss)
        
        
    def forward(self, out, clusters, groups):
        """
        out:
            dictionary output from GNN Model
            keys:
                'edge_pred': predicted edge weights from model forward
        data:
            data[0] - DBSCAN data
            data[1] - groups data
        """
        edge_ct = 0
        total_loss, total_acc = 0., 0.
        ari, ami, sbd, pur, eff = 0., 0., 0., 0., 0.
        ngpus = len(clusters)
        for i in range(ngpus):
            edge_pred = out['edge_pred'][i]
            data0 = clusters[i]
            data1 = groups[i]

            device = data0.device

            # first decide what true edges should be
            # need to form graph, then pass through GNN
            # clusts = form_clusters(data0)
            clusts = form_clusters_new(data0)


            # remove compton clusters
            # if no cluster fits this condition, return
            if self.remove_compton:
                selection = filter_compton(clusts, self.compton_thresh) # non-compton looking clusters
                if not len(selection):
                    ttotal_loss += self.lossfn(edge_pred, edge_pred)
                    totalacc += 1.
                    continue

                clusts = clusts[selection]

            # process group data
            # data_grp = process_group_data(data1, data0)
            data_grp = data1

            # form graph
            batch = get_cluster_batch(data0, clusts)
            edge_index = complete_graph(batch, device=device)

            if not edge_index.shape[0]:
                total_loss += self.lossfn(edge_pred, edge_pred)
                totalacc += 1.
                continue
                
            group = get_cluster_label(data_grp, clusts)

            # determine true assignments
            edge_assn = edge_assignment(edge_index, batch, group, device=device, dtype=torch.long)

            edge_assn = edge_assn.view(-1)

            # total loss on batch
            total_loss = self.lossfn(edge_pred, edge_assn)

            # compute assigned clusters
            fe = edge_pred[1,:] - edge_pred[0,:]
            cs = assign_clusters_UF(edge_index, fe, len(clusts), thresh=0.0)

            ari0, ami0, sbd0, pur0, eff0 = DBSCAN_cluster_metrics2(
                cs,
                clusts,
                group
            )
            ari += ari0
            ami += ami0
            sbd += sbd0
            pur += pur0
            eff += eff0

            edge_ct += edge_index.shape[1]
        
        return {
            'ARI': ari/ngpus,
            'AMI': ami/ngpus,
            'SBD': sbd/ngpus,
            'purity': pur/ngpus,
            'efficiency': eff/ngpus,
            'accuracy': total_acc/ngpus,
            'loss': total_loss/ngpus,
            'edge_count': edge_ct
        }
