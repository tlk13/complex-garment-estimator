import os
from pathlib import Path
import requests
import time
import json

import torch
import wandb as wb

# My
import customconfig
import data
import nets


# -------- Working with existing experiments ------
# TODO use in all subroutines
def load_experiment(
        name, run_id, project='Garments-Reconstruction', 
        in_data_folders=None, in_datapath=None,
        in_batch_size=None, in_device=None, checkpoint_idx=None):
    """
        Load information (dataset, wrapper, model) associated with a particular experiment

        Parameters re-write corresponding information from experiment, if set

        NOTE: if in_data_folders is provided then all data is loaded in a single loader instead of being splitted
            (since original split may not make sense for new data folders )
    """

    system_info = customconfig.Properties('./system.json')
    experiment = ExperimentWrappper(
        system_info['wandb_username'],
        project_name=project, 
        run_name=name, 
        run_id=run_id)  # finished experiment

    if not experiment.is_finished():
        print('Warning::Evaluating unfinished experiment')

    # -------- data -------
    # data_config also contains the names of datasets to use
    split, batch_size, data_config = experiment.data_info()  
    if in_batch_size is not None:
        batch_size = in_batch_size
    if in_data_folders is not None:
        data_config.update(data_folders=in_data_folders)
        split = None  # Just use all the data if the base set of datafolders is updated

    data_path = in_datapath if in_datapath is not None else system_info['datasets_path']


    data_class = getattr(data, data_config['class'] if 'class' in data_config else 'Garment3DPatternFullDataset')  # TODO check if it works!!
    dataset = data_class(data_path, data_config, gt_caching=True, feature_caching=True)
    datawrapper = data.DatasetWrapper(dataset, known_split=split, batch_size=batch_size)

    # ----- Model -------
    nn_config = experiment.NN_config()
    model_class = getattr(nets, nn_config['model'])
    model = model_class(dataset.config, nn_config, nn_config['loss'])
 
    device = in_device if in_device is not None else nn_config['device_ids'][0]
    model = torch.nn.DataParallel(model, device_ids=[device])

    # TODO propagate device decision to the data at evaluation time, if not using Data Parallel?
    
    if checkpoint_idx is not None: 
        state_dict = experiment.load_checkpoint_file(version=checkpoint_idx, device=device)['model_state_dict'] 
    else:
        state_dict = experiment.load_best_model(device=device)['model_state_dict']
    
    model.load_state_dict(state_dict)

    return datawrapper, model, experiment


# ------- Class for experiment tracking with wandb -------
class ExperimentWrappper(object):
    """Class provides 
        * a convenient way to store & load experiment info with integration to wandb 
        * some functions & params shortcuts to access wandb functionality
        * for implemented functions, transparent workflow for finished and active (initialized) run 
        * for finished experiments, if w&b run info is specified, it has priority over the localy stored information

        Wrapper currently does NOT wrap one-liners routinely called for active runs like wb.log(), wb.watch()  
    """
    # TODO propagate signature change to all usages
    def __init__(self, config, wandb_username='', no_sync=False):
        """Init experiment tracking with wandb
            With no_sync==True, run won't sync with wandb cloud. 
            Note that resuming won't work for off-cloud runs as it requiers fetching files from the cloud"""

        self.checkpoint_filetag = 'checkpoint'
        self.final_filetag = 'fin_model_state'
        self.wandb_username = wandb_username
        
        self.project = config['experiment'].get('project_name', None)
        self.run_name = config['experiment'].get('run_name', None)
        self.run_id = config['experiment'].get('run_id', None)
        self.no_sync = no_sync

        self.in_config = config

        # cannot use wb.config, wb.run, etc. until run initialized & logging started & local path
        self.initialized = False  
        self.artifact = None

    # ----- start&stop ------
    def init_run(self, config={}):
        """Start wandb logging. 
            If run_id is known, run is automatically resumed.
            """
        if self.no_sync:
            os.environ['WANDB_MODE'] = 'dryrun'
            print('Experiment:Warning: run is not synced with wandb cloud')

        wb.init(name=self.run_name, project=self.project, config=config, resume=self.run_id)
        self.run_id = wb.run.id

        self.initialized = True
        self.checkpoint_counter = 0

    def stop(self):
        """Stop wandb for current run. All logging finishes & files get uploaded"""
        if self.initialized:
            wb.finish()
        self.initialized = False

    # -------- run info ------
    def full_name(self):
        name = self.project if self.project else ''
        name += ('-' + self.run_name) if self.run_name else ''
        if self.run_id:
            name += ('-' + self.run_id)
        else:
            name += self.in_config['NN']['pre-trained'] if 'pre-trained' in self.in_config['NN'] else ''
        
        return name

    def last_epoch(self):
        """Id of the last epoch processed
            NOTE: part of resuming functionality, only for wandb runs
        """
        run = self._run_object()
        return run.summary['epoch'] if 'epoch' in run.summary else -1

    def data_info(self):
        """Info on the data setup from the run config:
            Split & batch size info """
        config = self._run_config()
        split_config = config['data_split']
        data_config = config['dataset']

        try:
            self.load_file('data_split.json', './wandb')
            split_config['filename'] = './wandb/data_split.json'
            # NOTE!!!! this is a sub-optimal workaround fix since the proper fix would require updates in class archtecture
            data_config['max_datapoints_per_type'] = None   # avoid slicing for correct loading of split on any machine
        except (ValueError, RuntimeError) as e:  # if file not found, training will just proceed with generated split
            print(e)
            print(f'{self.__class__.__name__}::Warning::Skipping loading split file from cloud..')
        
        try:
            self.load_file('panel_classes.json', './wandb')
            data_config['panel_classification'] = './wandb/panel_classes.json'
        except (ValueError, RuntimeError) as e:  # if file not found, training will just proceed with generated split
            print(e)
            print(f'{self.__class__.__name__}::Warning::Skipping loading panel classes file from cloud..')

        try:
            self.load_file('param_filter.json', './wandb')
            data_config['filter_by_params'] = './wandb/param_filter.json'
        except (ValueError, RuntimeError) as e:  # if file not found, training will just proceed with given setup
            print(e)
            print(f'{self.__class__.__name__}::Warning::Skipping loading parameter filter file from cloud..')
        
        return split_config, config['trainer']['batch_size'] if 'trainer' in config else config['batch_size'], data_config

    def last_best_validation_loss(self):
        """Id of the last epoch processed
            NOTE: only for experiments running\loaded from wandb
        """
        run = self._run_object()
        return run.summary['best_valid_loss'] if 'best_valid_loss' in run.summary else None

    def NN_config(self):
        """Run configuration params of NeuralNetwork model"""
        config = self._run_config()
        return config['NN']

    def add_statistic(self, tag, info, log=''):
        """Add info the run summary (e.g. stats on test set)"""
        
        # Log
        if log:
            print(f'{self.__class__.__name__}::Saving statistic {log}:')
            print(json.dumps(info, sort_keys=True, indent=2) if isinstance(info, dict) else info)

        if not self.run_id:
            print(f'{self.__class__.__name__}::Warning::Experiment not connected to the cloud. Statistic {tag} not synced')
            return 

        # different methods for on-going & finished runs
        if self.initialized:
            wb.run.summary[tag] = info
        else:
            if isinstance(info, dict):
                # NOTE Related wandb issue: https://github.com/wandb/client/issues/1934
                for key in info:
                    self.add_statistic(tag + '.' + key, info[key])
            else:
                run = self._run_object()
                run.summary[tag] = info
                run.summary.update()
            
    def add_config(self, tag, info):
        """Add new value to run config. Only for ongoing runs!"""
        if self.initialized:
            wb.config[tag] = info
        else:
            raise RuntimeError('ExperimentWrappper:Error:Cannot add config to finished run')

    def add_artifact(self, path, name, type):
        """Create a new wandb artifact and upload all the contents there"""
        if not self.run_id:
            print(f'{self.__class__.__name__}::Warning::Experiment not connected to the cloud. Artifact {name} not synced')
            return 

        path = Path(path)

        if not self.initialized:
            # can add artifacts only to existing runs!
            # https://github.com/wandb/client/issues/1491#issuecomment-726852201
            print('Experiment::Reactivating wandb run to upload an artifact {}!'.format(name))
            wb.init(id=self.run_id, project=self.project, resume='allow')

        artifact = wb.Artifact(name, type=type)
        if path.is_file():
            artifact.add_file(str(path))
        else:
            artifact.add_dir(str(path))
                    
        wb.run.log_artifact(artifact)

        if not self.initialized:
            wb.finish()

    def is_finished(self):
        if not self.run_id:
            print(f'{self.__class__.__name__}::Warning::Requested status of run not connected to wandb')
            return True
        run = self._run_object()
        return run.state == 'finished'

    # ---- file info -----
    def checkpoint_filename(self, check_id=None):
        """Produce filename for the checkpoint of given epoch"""
        check_id_str = '_{}'.format(check_id) if check_id is not None else ''
        return '{}{}.pth'.format(self.checkpoint_filetag, check_id_str)

    def artifactname(self, tag, with_version=True, version=None, custom_alias=None):
        """Produce name for wandb artifact for current run with fiven tag"""
        basename = self.run_name + '_' + self.run_id + '_' + tag
        if custom_alias is not None:
            return basename + ':' + custom_alias

        # else -- return a name with versioning
        version_tag = ':v' + str(version) if version is not None else ':latest'
        return basename + version_tag if with_version else basename

    def final_filename(self):
        """Produce filename for the final model file (assuming PyTorch)"""
        return self.final_filetag + '.pth'

    def cloud_path(self):
        """Return virtual path to the current run on wandb could
            Implemented as a function to allow dynamic update of components with less bugs =)
        """
        if not self.run_id:
            raise RuntimeError('ExperimentWrappper:Error:Need to know run id to get path in wandb could')
        return self.wandb_username + '/' + self.project + '/' + self.run_id

    def local_wandb_path(self):
        # if self.initialized:
        return Path(wb.run.dir)
        # raise RuntimeError('ExperimentWrappper:Error:No local path exists: run is not initialized')

    def local_artifact_path(self):
        """create & maintain path to save files to-be-commited-as-artifacts"""
        path = Path('./wandb') / 'artifacts' / self.run_id
        if not path.exists():
            path.mkdir(parents=True)
        return path

    # ----- working with files -------
    def load_checkpoint_file(self, to_path=None, version=None, device=None):
        """Load checkpoint file for given epoch from the cloud"""
        if not self.run_id:
            raise RuntimeError('ExperimentWrappper:Error:Need to know run id to restore specific checkpoint from the could')
        try:
            art_path = self._load_artifact(self.artifactname('checkpoint', version=version), to_path=to_path)
            for file in art_path.iterdir(): # only one file per checkpoint anyway
                return self._load_model(file, device)

        except (RuntimeError, requests.exceptions.HTTPError, wb.apis.CommError) as e:  # raised when file is corrupted or not found
            print('ExperimentWrappper::Error::checkpoint from version \'{}\'is corrupted or lost: {}'.format(version if version else 'latest', e))
            raise e
    
    def load_best_model(self, to_path=None, device=None):
        """Load model parameters (model with best performance) file from the cloud or from locally saved model file if it exists

            NOTE: cloud has a priority as it might contain up-to-date information
        """
        if not self.run_id:
            if 'pre-trained' in self.in_config['NN']:
                # local model available
                print(f'{self.__class__.__name__}::Info::Loading locally saved model')
                return self._load_model(self.in_config['NN']['pre-trained'], device)
            else:
                raise RuntimeError('ExperimentWrappper:Error:Need to know run_id to restore best model from the could OR path to the locally saved model ')
        try:
            # best checkpoints have 'best' alias
            art_path = self._load_artifact(self.artifactname('checkpoint', custom_alias='best'), to_path=to_path)
            for file in art_path.iterdir():
                print(file)
                if device is not None:
                    return torch.load(file, map_location=device)
                else: 
                    return torch.load(file)  # to the same device it was saved from
                # only one file per checkpoint anyway

        except (requests.exceptions.HTTPError):  # file not found
            raise RuntimeError('ExperimentWrappper:Error:No file with best weights found in run {}'.format(self.cloud_path()))
    
    def save_checkpoint(self, state, aliases=[], wait_for_upload=False):
        """Save given state dict as torch checkpoint to local run dir
            aliases assign labels to checkpoints for easy retrieval

            NOTE: only for active wandb runs
        """

        if not self.initialized:
            # prevent training updated to finished runs
            raise RuntimeError('Experiment::cannot save checkpoint files to non-active wandb runs')

        print('Experiment::Saving model state -- checkpoint artifact')

        # Using artifacts to store important files for this run
        filename = self.checkpoint_filename(self.checkpoint_counter)
        artifact = wb.Artifact(self.artifactname('checkpoint', with_version=False), type='checkpoint')
        self.checkpoint_counter += 1  # ensure all checkpoints have unique names 

        torch.save(state, self.local_artifact_path() / filename)
        artifact.add_file(str(self.local_artifact_path() / filename))
        wb.run.log_artifact(artifact, aliases=['latest'] + aliases)

        if wait_for_upload:
            self._wait_for_upload(self.artifactname('checkpoint', version=self.checkpoint_counter - 1))

    def load_file(self, filename, to_path='.'):
        """Download a file from the wandb experiment to given path or to currect directory"""
        if not self.run_id:
            raise RuntimeError('ExperimentWrappper:Error:Need to know run id to restore a file from the could')
        wb.restore(filename, run_path=self.project + '/' + self.run_id, replace=True, root=to_path)

    # ------- utils -------
    def _load_artifact(self, artifact_name, to_path=None):
        """Download a requested artifact withing current project. Return loaded path"""
        print('Experiment::Requesting artifacts: {}'.format(artifact_name))

        api = wb.Api({'project': self.project})
        artifact = api.artifact(name=artifact_name)
        filepath = artifact.download(str(to_path) if to_path else None)
        print('Experiment::Artifact saved to: {}'.format(filepath))

        return Path(filepath)

    def _run_object(self):
        """ Shortcut for getting reference to wandb api run object. 
            To uniformly access both ongoing & finished runs"""
        return wb.Api().run(self.cloud_path())
    
    def _run_config(self):
        """Shortcut for getting run configuration information"""
        if self.run_id:
            run = wb.Api().run(self.cloud_path())
            return run.config
        else:
            return self.in_config

    def _wait_for_upload(self, artifact_name, max_attempts=10):
        """Wait for an upload of the given version of an artifact"""
        # follows the suggestion of https://github.com/wandb/client/issues/1486#issuecomment-726229978
        print('Experiment::Waiting for artifact {} upload'.format(artifact_name))
        attempt = 1
        while attempt <= max_attempts:
            try:
                time.sleep(5)
                self._load_artifact(artifact_name)
                print('Requested version is successfully syncronized')
                break
            except (ValueError, wb.CommError):
                attempt += 1
                print('Trying again')
        if attempt > max_attempts:
            print('Experiment::Warning::artifact {} is still not syncronized'.format(artifact_name))

    def _load_model(self, file, device=None):
        print(file)
        if device is not None:
            return torch.load(file, map_location=device)
        else: 
            return torch.load(file)  # to the same device it was saved from