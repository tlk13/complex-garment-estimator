"""Interpolate sewing patterns from the two input 3D garments"""

import argparse
from datetime import datetime
import igl
import numpy as np
from pathlib import Path
import shutil
import torch

# Do avoid a need for changing Evironmental Variables outside of this script
import os,sys,inspect
currentdir = os.path.dirname(os.path.realpath(__file__) )
parentdir = os.path.dirname(currentdir)
sys.path.insert(0,parentdir) 

# My modules
import customconfig, nets, data
from experiment import WandbRunWrappper
from pattern.wrappers import VisPattern
from data import GarmentBaseDataset


def lerp(t, low, high):
    return (1 - t) * low + t * high


def slerp(t, low, high):
    """
        Spherical Linear interpolation
        https://machinelearningmastery.com/how-to-interpolate-and-perform-vector-arithmetic-with-faces-using-a-generative-adversarial-network/
    """
    omega = torch.arccos(torch.clamp(torch.dot(low / torch.norm(low, 2), high / torch.norm(high, 2)), -1, 1))
    so = torch.sin(omega)
    if so == 0:
        # L'Hopital's rule/LERP
        return (1.0 - t) * low + val * high
    return sin((1.0 - t) * omega) / so * low + sin(t * omega) / so * high


if __name__ == "__main__":
    
    system_info = customconfig.Properties('./system.json')
    device = 'cuda:0' if torch.cuda.is_available() else 'cpu'
    save_to = Path(system_info['output']) / ('interpolate' + '_' + datetime.now().strftime('%y%m%d-%H-%M-%S'))
    save_to.mkdir(parents=True)

    # exactly 2
    mesh_paths = [
        Path(system_info['datasets_path']) / 'data_uni_1000_pants_straight_sides_210105-10-49-02' / 'pants_straight_sides_5JAL3POBNX' / 'pants_straight_sides_5JAL3POBNX_sim.obj', 
        Path(system_info['datasets_path']) / 'data_uni_300_tee_sleeveless_210311-14-04-37' / 'tee_sleeveless_3I7NA1HAZG' / 'tee_sleeveless_3I7NA1HAZG_sim.obj',
    ]
    num_in_between = 5

    # --------------- Experiment to evaluate on ---------
    experiment = WandbRunWrappper(
        system_info['wandb_username'],
        project_name='Garments-Reconstruction', 
        run_name='teesl-pants-Jump-300-server', 
        run_id='311kha7h')  # finished experiment
    if not experiment.is_finished():
        print('Warning::Evaluating unfinished experiment')

    # data stats from training 
    _, _, data_config = experiment.data_info()  # need to get data stats

    # ----- Model architecture -----
    model = nets.GarmentFullPattern3D(data_config, experiment.NN_config())
    model.load_state_dict(experiment.load_best_model()['model_state_dict'])
    model = model.to(device=device)
    model.eval()

    # ------ prepare input data & construct batch -------
    points_list = data.sample_points_from_meshes(mesh_paths, data_config)

    # -------- Interpolation ---------
    # Encode
    with torch.no_grad():
        points_batch = torch.stack(points_list).to(device)
        pred_encodings = model.forward_encode(points_batch)

    # Interpolate
    encodings = []
    t = 0
    encodings.append(pred_encodings[0])
    for i in range(num_in_between + 1):
        t += 1. / (num_in_between + 1)
        encodings.append(
            lerp(t, pred_encodings[0], pred_encodings[1])
        )
    encodings = torch.stack(encodings).to(device)
    # encodings = pred_encodings

    # print(encodings)

    # decode
    with torch.no_grad():
        preds = model.forward_decode(encodings)

    # ---- save ----
    names = ['t_{:.2f}'.format(i / (num_in_between + 1)) for i in range(num_in_between + 2)]

    data.save_garments_prediction(preds, save_to, data_config, names)
