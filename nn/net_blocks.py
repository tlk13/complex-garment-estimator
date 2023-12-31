"""Basic building blocks for custom neural network architectures"""
import torch
import torch.nn as nn
import torch_geometric.nn as geometric


# --------- PointNet++ modules -------
# https://github.com/rusty1s/pytorch_geometric/blob/master/examples/pointnet2_classification.py

class _SetAbstractionModule(nn.Module):
    """Performes PointNet feature extraction in local areas (around sampled centroids) of given sets"""
    def __init__(self, ratio, conv_radius, per_point_nn):
        super().__init__()
        self.ratio = ratio  # for controlling number of centroids
        self.radius = conv_radius
        self.conv = geometric.PointConv(per_point_nn)

    def forward(self, features, pos, batch):
        idx = geometric.fps(pos, batch, ratio=self.ratio)
        row, col = geometric.radius(pos, pos[idx], self.radius, batch, batch[idx],
                                    max_num_neighbors=25)
        edge_index = torch.stack([col, row], dim=0)
        features = self.conv(features, (pos, pos[idx]), edge_index)
        pos, batch = pos[idx], batch[idx]
        return features, pos, batch


class _GlobalSetAbstractionModule(nn.Module):
    """PointNet feature extraction to get one deature vector from set"""
    def __init__(self, per_point_net):
        super().__init__()
        self.nn = per_point_net

    def forward(self, features, pos, batch):
        features = torch.cat([features, pos], dim=1) if features is not None else pos
        features = self.nn(features)
        features = geometric.global_max_pool(features, batch)  # returns classical PyTorch batch format #Batch_size x (out_shape)
        pos = pos.new_zeros((features.size(0), 3))
        batch = torch.arange(features.size(0), device=batch.device)
        return features, pos, batch


def MLP(channels, batch_norm=True):
    return nn.Sequential(*[
        nn.Sequential(nn.Linear(channels[i - 1], channels[i]), nn.ReLU(), nn.BatchNorm1d(channels[i]))
        for i in range(1, len(channels))
    ])


class PointNetPlusPlus(nn.Module):
    """
        Module for extracting latent representation of 3D geometry.
        Based on PointNet++
        NOTE architecture is agnostic of number of input points
    """
    def __init__(self, out_size, config={}):
        super().__init__()

        self.config = {'r1': 0.3, 'r2': 0.4, 'r3': 5, 'r4': 7}  # defaults for this net
        self.config.update(config)  # from input

        self.sa1_module = _SetAbstractionModule(0.2, self.config['r1'], MLP([3, self.config['EConv_hidden'], self.config['EConv_hidden'], self.config['EConv_feature']]))
        # self.sa2_module = _SetAbstractionModule(0.25, self.config['r2'], MLP([112 + 3, 200, 200, 112]))
        # self.sa3_module = _SetAbstractionModule(0.25, self.config['r3'], MLP([128 + 3, 128, 128, 128]))
        # self.sa4_module = _SetAbstractionModule(0.25, self.config['r4'], MLP([128 + 3, 128, 128, 256]))
        self.sa_last_module = _GlobalSetAbstractionModule(MLP([3 + self.config['EConv_feature'], self.config['EConv_hidden'], self.config['EConv_hidden'], self.config['EConv_feature']]))
        # self.sa_last_module = _GlobalSetAbstractionModule(MLP([3, 256, 512, 1024]))

        self.lin = nn.Linear(self.config['EConv_feature'], out_size)

    def forward(self, positions):

        # flatten the batch for torch-geometric batch format
        pos_flat = positions.view(-1, positions.size(-1))
        batch = torch.cat([
            torch.full((elem.size(0),), fill_value=i, device=positions.device, dtype=torch.long) for i, elem in enumerate(positions)
        ])

        # forward pass
        sa_out = (None, pos_flat, batch)
        sa_out = self.sa1_module(*sa_out)
        # sa_out = self.sa2_module(*sa_out)
        # sa3_out = self.sa3_module(*sa2_out)
        # sa4_out = self.sa4_module(*sa3_out)
        sa_last_out = self.sa_last_module(*sa_out)
        out, _, _ = sa_last_out
        out = self.lin(out)
        return out


# ------------- EdgeConv ----------
# https://github.com/AnTao97/dgcnn.pytorch/blob/master/model.py
class EdgeConvFeatures(nn.Module):
    """Extracting feature vector from 3D point cloud based on Edge convolutions from Paper “Dynamic Graph CNN for Learning on Point Clouds”"""
    def __init__(self, out_size, config={}):
        super().__init__()

        self.config = {
            'conv_depth': 2, 
            'k_neighbors': 5, 
            'EConv_hidden': 200, 
            'EConv_hidden_depth': 2, 
            'EConv_feature': 112, 
            'EConv_aggr': 'max', 
            'global_pool': 'mean', 
            'skip_connections': False, 
            'graph_pooling': False,
            'pool_ratio': 0.1  # only used when the graph pooling is enabled
        }  # defaults for this net
        self.config.update(config)  # from input

        # Node feature sizes scheme & MLP hidden layers scheme
        if self.config['graph_pooling']:
            # use size variation when using graph pooling to enable larger layers under the same memory constraint
            features_by_layer = [int(self.config['EConv_feature'] / conv_id) for conv_id in range(self.config['conv_depth'], 0, -1)]
            hidden_by_layer = [int(self.config['EConv_hidden'] / conv_id) for conv_id in range(self.config['conv_depth'], 0, -1)]
        else:
            features_by_layer = [self.config['EConv_feature'] for _ in range(self.config['conv_depth'])]
            hidden_by_layer = [self.config['EConv_hidden'] for _ in range(self.config['conv_depth'])]
        mlp_depth = self.config['EConv_hidden_depth']

        # Contruct the net
        # Conv layers
        self.conv_layers = nn.ModuleList()
        # first is always there
        self.conv_layers.append(
            geometric.DynamicEdgeConv(
                MLP([2 * 3] + [hidden_by_layer[0] for _ in range(mlp_depth)] + [features_by_layer[0]]), 
                k=self.config['k_neighbors'], aggr=self.config['EConv_aggr']))

        for conv_id in range(1, self.config['conv_depth']):
            self.conv_layers.append(
                geometric.DynamicEdgeConv(
                    MLP([2 * features_by_layer[conv_id - 1]] + [hidden_by_layer[conv_id] for _ in range(mlp_depth)] + [features_by_layer[conv_id]]), 
                    k=self.config['k_neighbors'], aggr=self.config['EConv_aggr']))

        # pooling layers
        if self.config['graph_pooling']:
            self.gpool_layers = nn.ModuleList()
            for conv_id in range(0, self.config['conv_depth']):
                self.gpool_layers.append(
                    DynamicASAPool(features_by_layer[conv_id], k=self.config['k_neighbors'], pool_ratio=self.config['pool_ratio']))

        # global pooling layer based on config
        if self.config['global_pool'] == 'max':
            self.global_pool = geometric.global_max_pool
        elif self.config['global_pool'] == 'mean':
            self.global_pool = geometric.global_mean_pool
        elif self.config['global_pool'] == 'add':
            self.global_pool = geometric.global_add_pool
        else:  # max
            raise ValueError('{} pooling is not supported'.format(self.config['global_pool']))
            
        # Output linear layer
        # NOTE Skip connection is only for initial position
        out_features = self.config['EConv_feature'] + 3 if self.config['skip_connections'] else self.config['EConv_feature']

        self.lin = nn.Linear(out_features, out_size)

    def forward(self, positions, global_pool=True):
        batch_size = positions.size(0)
        n_vertices = positions.size(1)
        # flatten the batch for torch-geometric batch format
        pos_flat = positions.view(-1, positions.size(-1))
        batch = torch.cat([
            torch.full((elem.size(0),), fill_value=i, device=positions.device, dtype=torch.long) for i, elem in enumerate(positions)
        ])

        # Vertex features + track global features from each layer (if skip connections are used)
        # In EdgeConv features from different layers are concatenated per node and then aggregated 
        # but since the pooling is element-wise on feature vectors, we can swap the operations to save memory
        out = pos_flat  
        for conv_id in range(0, self.config['conv_depth']):
            out = self.conv_layers[conv_id](out, batch)
            if self.config['graph_pooling']:
                out, batch = self.gpool_layers[conv_id](out, batch)
        
        if self.config['skip_connections']:
            # concat positions and final features
            out = torch.cat([out, pos_flat], dim=-1)
        
        if global_pool:
            # 'out' now holds per-point features
            pooled_feature = self.global_pool(out, batch, batch_size)

            # post-processing
            encoding = self.lin(pooled_feature)

            return encoding, out, batch
        else:
            return None, out, batch


class DynamicASAPool(nn.Module):
    """Pooling operator on PointCloud-like feature input that constructs the graph from imput point features
        and performes  Self-Attention Graph Pooling on it
        https://pytorch-geometric.readthedocs.io/en/latest/modules/nn.html#torch_geometric.nn.pool.SAGPooling

        * k -- number of nearest neighbors to use for building the graph == pooling power~!
    """
    def __init__(self, feature_size, k=10, pool_ratio=0.5):
        super().__init__()

        self.k = 10
        self.edge_pool = geometric.ASAPooling(feature_size, ratio=pool_ratio)

    def forward(self, node_features, batch):

        # graph construction 
        # follows the idea from here https://pytorch-geometric.readthedocs.io/en/latest/_modules/torch_geometric/nn/conv/edge_conv.html#DynamicEdgeConv
        if isinstance(batch, torch.Tensor):  # or, ot could be a tuple already
            b = (batch, batch)

        edge_index = geometric.knn(node_features, node_features, self.k, b[0], b[1])

        out, edge_index, _, new_batch, _ = self.edge_pool(node_features, edge_index, batch=batch)

        return out, new_batch


class EdgeConvPoolingFeatures(nn.Module):
    """Extracting feature vector from 3D point cloud based on Edge convolutions from Paper “Dynamic Graph CNN for Learning on Point Clouds”
        with added pooling layers that coarsen the graphs and (should) give additional feature propagation"""
    def __init__(self, out_size, config={}):
        super().__init__()

        self.config = {'conv_depth': 3}  # defaults for this net
        self.config.update(n_features1=32, n_features2=128, n_features3=256, k=10)
        self.config.update(config)  # from input

        self.conv1 = geometric.DynamicEdgeConv(
            MLP([2 * 3, 64, 64, self.config['n_features1']]), 
            k=self.config['k'], aggr='max')
        self.pool1 = DynamicASAPool(self.config['n_features1'], k=self.config['k'])
        self.conv2 = geometric.DynamicEdgeConv(
            MLP([2 * self.config['n_features1'], self.config['n_features2'], self.config['n_features2'], self.config['n_features2']]), 
            k=self.config['k'], aggr='max'
        )
        self.pool2 = DynamicASAPool(self.config['n_features2'], k=self.config['k'])
        self.conv3 = geometric.DynamicEdgeConv(
            MLP([2 * self.config['n_features2'], self.config['n_features3'], self.config['n_features3'], self.config['n_features3']]), 
            k=self.config['k'], aggr='max')

        self.lin = nn.Linear(self.config['n_features3'], out_size)

    def forward(self, positions):
        # flatten the batch for torch-geometric batch format
        pos_flat = positions.view(-1, positions.size(-1))
        batch = torch.cat([
            torch.full((elem.size(0),), fill_value=i, device=positions.device, dtype=torch.long) for i, elem in enumerate(positions)
        ])

        # Vertex features
        out = self.conv1(pos_flat, batch)
        out, batch = self.pool1(out, batch)

        out = self.conv2(out, batch)
        out, batch = self.pool2(out, batch)

        out = self.conv3(out, batch)

        # aggregate features from final nodes
        out = geometric.global_max_pool(out, batch)

        # post-processing
        out = self.lin(out)

        return out


# ------------ MLP Decoder -------------

class MLPDecoder(nn.Module):
    """ MLP-based decoding of latent code -> sequence of elements
        Assuming the max length of sequence is known
    """
    def __init__(
            self, 
            encoding_size, hidden_size, out_elem_size, n_layers, out_len=1,
            dropout=0, custom_init='kaiming_normal'):
        super().__init__()
        self.out_len = out_len

        self.mlp = MLP([encoding_size] + [hidden_size * out_len for _ in range(n_layers)] + [out_elem_size * out_len])

        # initialize
        _init_weights(self.mlp, init_type=custom_init)

    def forward(self, batch_enc, *args):
        """out_len specifies the length of the output sequence to produce"""
        batch_size = batch_enc.size(0)
        
        out = self.mlp(batch_enc)

        # convert to sequence
        out = out.contiguous().view(batch_size, self.out_len, -1)

        return out


# ------------- Sequence modules -----------
def _init_tenzor(*shape, device='cpu', init_type=''):
    """shortcut to create & initialize tenzors on a given device.  """
    # TODO suport other init types 
    if not init_type or len(shape) == 1:  
        # zeros by default
        # and in case of 1D tensor (which kaiming_normal does not support)
        new_tenzor = torch.zeros(shape)
    elif 'kaiming_normal' in init_type:
        new_tenzor = torch.empty(shape)
        nn.init.kaiming_normal_(new_tenzor)
    else:  
        raise NotImplementedError('{} tenzor initialization is not implemented'.format(init_type))

    return new_tenzor.to(device)


def _init_weights(module, init_type=''):
    """Initialize weights of provided module with requested init type"""
    if not init_type:
        # do not re-initialize, leave default pytorch init
        return
    for name, param in module.named_parameters():
        if 'weight' in name:
            
            if 'kaiming_normal' in init_type:
                if len(param.shape) == 1:  # case not supported by kaiming_normal
                    param = torch.zeros(param.shape, device=param.device)
                else:
                    nn.init.kaiming_normal_(param)
            else:
                raise NotImplementedError('{} weight initialization is not implemented'.format(init_type))
    # leave defaults for bias


class LSTMEncoderModule(nn.Module):
    """A wrapper for LSTM targeting encoding task"""
    def __init__(self, elem_len, encoding_size, n_layers, dropout=0, custom_init='kaiming_normal'):
        super().__init__()
        self.custom_init = custom_init
        self.n_layers = n_layers
        self.encoding_size = encoding_size

        self.lstm = nn.LSTM(
            elem_len, encoding_size, n_layers, 
            dropout=dropout, batch_first=True)

        _init_weights(self.lstm, init_type=custom_init)

    def forward(self, batch_sequence):
        device = batch_sequence.device
        batch_size = batch_sequence.size(0)
        
        # --- encode --- 
        hidden_init = _init_tenzor(self.n_layers, batch_size, self.encoding_size, device=device, init_type=self.custom_init)
        cell_init = _init_tenzor(self.n_layers, batch_size, self.encoding_size, device=device, init_type=self.custom_init)
        _, (hidden, _) = self.lstm(batch_sequence, (hidden_init, cell_init))

        # final encoding is the last output == hidden of last layer 
        return hidden[-1]


class LSTMDecoderModule(nn.Module):
    """A wrapper for LSTM targeting decoding task"""
    def __init__(self, encoding_size, hidden_size, out_elem_size, n_layers, dropout=0, custom_init='kaiming_normal', **kwargs):
        super().__init__()
        self.custom_init = custom_init
        self.n_layers = n_layers
        self.encoding_size = encoding_size
        self.hidden_size = hidden_size
        self.out_elem_size = out_elem_size

        self.lstm = nn.LSTM(encoding_size, hidden_size, n_layers, 
                            dropout=dropout, batch_first=True)

        # post-process to match the desired outut shape
        self.lin = nn.Linear(hidden_size, out_elem_size)

        # initialize
        _init_weights(self.lstm, init_type=custom_init)

    def forward(self, batch_enc, out_len):
        """out_len specifies the length of the output sequence to produce"""
        device = batch_enc.device
        batch_size = batch_enc.size(0)
        
        # propagate encoding for needed seq_len
        dec_input = batch_enc.unsqueeze(1).repeat(1, out_len, 1)  # along sequence dimention

        # decode
        hidden_init = _init_tenzor(self.n_layers, batch_size, self.hidden_size, device=device, init_type=self.custom_init)
        cell_init = _init_tenzor(self.n_layers, batch_size, self.hidden_size, device=device, init_type=self.custom_init)
        out, _ = self.lstm(dec_input, (hidden_init, cell_init))
        
        # back to requested format
        # reshaping the outputs such that it can be fit into the fully connected layer
        out = out.contiguous().view(-1, self.hidden_size)
        out = self.lin(out)
        # back to sequence
        out = out.contiguous().view(batch_size, out_len, -1)

        return out


class LSTMDoubleReverseDecoderModule(nn.Module):
    """A wrapper for LSTM targeting decoding task that decodes the sequence in the reverse order, 
    and then processes it in the forward order to refine the reconstuction"""
    def __init__(self, encoding_size, hidden_size, out_elem_size, n_layers, dropout=0, custom_init='kaiming_normal', **kwargs):
        super().__init__()
        self.custom_init = custom_init
        self.n_layers = n_layers
        self.encoding_size = encoding_size
        self.hidden_size = hidden_size
        self.out_elem_size = out_elem_size

        # revrese & forward models share the architecture but not the weights
        self.lstm_reverse = nn.LSTM(encoding_size, hidden_size, n_layers, 
                                    dropout=dropout, batch_first=True)
        self.lstm_forward = nn.LSTM(hidden_size + encoding_size, hidden_size, n_layers, 
                                    dropout=dropout, batch_first=True)

        # post-process to match the desired outut shape
        self.lin = nn.Linear(hidden_size, out_elem_size)

        # initialize
        _init_weights(self.lstm_reverse, init_type=custom_init)
        _init_weights(self.lstm_forward, init_type=custom_init)

    def forward(self, batch_enc, out_len):
        """out_len specifies the length of the output sequence to produce"""
        device = batch_enc.device
        batch_size = batch_enc.size(0)
        
        # propagate encoding for needed seq_len
        dec_input = batch_enc.unsqueeze(1).repeat(1, out_len, 1)  # along sequence dimention

        # decode reversed sequence
        hidden_init = _init_tenzor(self.n_layers, batch_size, self.hidden_size, device=device, init_type=self.custom_init)
        cell_init = _init_tenzor(self.n_layers, batch_size, self.hidden_size, device=device, init_type=self.custom_init)
        out, state = self.lstm_reverse(dec_input, (hidden_init, cell_init))
        
        # decode forward sequence
        out = torch.flip(out, [1])
        out = torch.cat([out, dec_input], -1)  # skip connection with original input
        out, _ = self.lstm_forward(out, state)  # pass the state from previous module for additional info

        # back to requested format
        # reshaping the outputs such that it can be fit into the fully connected layer
        out = out.contiguous().view(-1, self.hidden_size)
        out = self.lin(out)
        # back to sequence
        out = out.contiguous().view(batch_size, out_len, -1)

        return out


class GRUDecoderModule(nn.Module):
    """A wrapper for LSTM targeting decoding task"""
    def __init__(self, encoding_size, hidden_size, out_elem_size, n_layers, dropout=0, custom_init='kaiming_normal', **kwargs):
        super().__init__()
        self.custom_init = custom_init
        self.n_layers = n_layers
        self.encoding_size = encoding_size
        self.hidden_size = hidden_size
        self.out_elem_size = out_elem_size

        self.recurrent_cell = nn.GRU(
            encoding_size, hidden_size, n_layers, 
            dropout=dropout, batch_first=True)

        # post-process to match the desired outut shape
        self.lin = nn.Linear(hidden_size, out_elem_size)

        # initialize
        _init_weights(self.recurrent_cell, init_type=custom_init)

    def forward(self, batch_enc, out_len):
        """out_len specifies the length of the output sequence to produce"""
        device = batch_enc.device
        batch_size = batch_enc.size(0)
        
        # propagate encoding for needed seq_len
        dec_input = batch_enc.unsqueeze(1).repeat(1, out_len, 1)  # along sequence dimention

        # decode
        # https://discuss.pytorch.org/t/gru-cant-deal-with-self-hidden-attributeerror-tuple-object-has-no-attribute-size/17283/2
        hidden_init = _init_tenzor(self.n_layers, batch_size, self.hidden_size, device=device, init_type=self.custom_init)
        out, _ = self.recurrent_cell(dec_input, hidden_init)

        # back to requested format
        # reshaping the outputs such that it can be fit into the fully connected layer
        out = out.contiguous().view(-1, self.hidden_size)
        out = self.lin(out)
        # back to sequence
        out = out.contiguous().view(batch_size, out_len, -1)

        return out


# Quick tests
if __name__ == "__main__":

    torch.manual_seed(125)

    positions = torch.arange(1, 601, dtype=torch.float)  # 37
    features_batch = positions.view(2, -1, 3)  # note for the same batch size
    # features_batch = positions.view(-1, 3) 

    print('In batch shape: {}'.format(features_batch.shape))

    net = EdgeConvFeatures(5)

    print(net(features_batch))
