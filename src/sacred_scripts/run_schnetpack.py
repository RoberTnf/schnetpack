import torch
import os
import yaml
import numpy as np
from shutil import rmtree
from sacred import Experiment
from model_ingredients import model_ingredient, build_model
from trainer_ingredients import train_ingredient, setup_trainer
from dataset_ingredients import dataset_ingredient, get_dataset, \
    get_property_map
from schnetpack.property_model import Properties
from schnetpack.data import AtomsLoader
from sacred.observers import MongoObserver


ex = Experiment('experiment', ingredients=[model_ingredient, train_ingredient,
                dataset_ingredient])


def is_extensive(prop):
    return prop == Properties.energy


@ex.config
def cfg():
    loss_tradeoff = {}
    overwrite = True
    additional_outputs = []

    modeldir = None
    batch_size = 100
    num_train = 0.8
    num_val = 0.1
    num_workers = 2
    mean = None
    stddev = None
    device = 'cpu'
    mongo_url = None
    mongo_db = None

    use_properties = []


@ex.named_config
def observe():
    mongo_url = 'mongodb://127.0.0.1:27017'
    mongo_db = 'test'
    ex.observers.append(MongoObserver.create(url=mongo_url,
                                             db_name=mongo_db))


@ex.named_config
def debug_config():
    modeldir = './models/debug'
    properties = ['energy', 'forces']


@ex.capture
def save_config(_config, modeldir):
    with open(os.path.join(modeldir, 'config.yaml'), 'w') as f:
        yaml.dump(_config, f, default_flow_style=False)


@ex.capture
def prepare_data(_seed, property_map,
                 batch_size, num_train, num_val, num_workers):
    # local seed
    np.random.seed(_seed)

    # load and split
    data = get_dataset(dataset_properties=property_map.values())

    if num_train < 1:
        num_train = int(num_train * len(data))
    if num_val < 1:
        num_val = int(num_val * len(data))

    train, val, test = data.create_splits(num_train, num_val)

    train_loader = AtomsLoader(train, batch_size, True, pin_memory=True,
                               num_workers=num_workers)
    val_loader = AtomsLoader(val, batch_size, False, pin_memory=True,
                             num_workers=num_workers)
    test_loader = AtomsLoader(test, batch_size, False, pin_memory=True,
                              num_workers=num_workers)

    atomrefs = {p: data.get_atomref(tgt)
                for p, tgt in property_map.items()
                if tgt is not None}

    return train_loader, val_loader, test_loader, atomrefs


@ex.capture
def stats(train_loader, atomrefs, property_map, mean, stddev, _config):
    props = [p for p, tgt in property_map.items() if tgt is not None]
    targets = [property_map[p] for p in props if
               p not in [Properties.polarizability, Properties.dipole_moment]]
    atomrefs = [atomrefs[p] for p in props if
                p not in [Properties.polarizability, Properties.dipole_moment]]
    extensive = [is_extensive(p) for p in props if
                 p not in [Properties.polarizability,
                           Properties.dipole_moment]]

    if len(targets) > 0:
        if mean is None or stddev is None:
            mean, stddev = train_loader.get_statistics(targets, extensive,
                                                       atomrefs)
            _config["mean"] = dict(
                zip(props, [m.detach().cpu().numpy().tolist() for m in mean]))
            _config["stddev"] = dict(
                zip(props, [m.detach().cpu().numpy().tolist() for m in stddev]))
    else:
        _config["mean"] = {}
        _config["stddev"] = {}
    return _config['mean'], _config['stddev']


@ex.capture
def create_modeldir(_log, modeldir, overwrite):
    _log.info("Create model directory")
    if modeldir is None:
        raise ValueError('Config `modeldir` has to be set!')

    if os.path.exists(modeldir) and not overwrite:
        raise ValueError(
            'Model directory already exists (set overwrite flag?):', modeldir)

    if os.path.exists(modeldir) and overwrite:
        rmtree(modeldir)

    if not os.path.exists(modeldir):
        os.makedirs(modeldir)


@ex.capture
def build_loss(property_map, loss_tradeoff):
    def loss_fn(batch, result):
        loss = 0.
        for p, tgt in property_map.items():
            if tgt is not None:
                diff = batch[tgt] - result[p]
                diff = diff ** 2
                err_sq = torch.mean(diff)
                if p in loss_tradeoff.keys():
                    err_sq *= loss_tradeoff[p]
                loss += err_sq
        return loss
    return loss_fn


@ex.command
def train(_log, _config, modeldir, properties, additional_outputs, device,
          num_train, num_val, num_workers, batch_size):
    property_map = get_property_map(properties)
    create_modeldir()
    save_config()

    _log.info("Load data")
    train_loader, val_loader, _, atomrefs = prepare_data(num_train=num_train,
                                                         num_val=num_val,
                                                         num_workers=num_workers,
                                                         batch_size=batch_size,
                                                         property_map=property_map)
    mean, stddev = stats(train_loader, atomrefs, property_map)

    _log.info("Build model")
    model_properties = [p for p, tgt in property_map.items() if tgt is not None]
    model = build_model(mean=mean, stddev=stddev, atomrefs=atomrefs,
                        model_properties=model_properties,
                        additional_outputs=additional_outputs).to(device)

    _log.info("Setup training")
    loss_fn = build_loss(property_map=property_map)
    trainer = setup_trainer(model=model, loss_fn=loss_fn, modeldir=modeldir,
                            train_loader=train_loader, val_loader=val_loader,
                            property_map=property_map)
    _log.info("Training")
    trainer.train(device)


@ex.command
def download():
    get_dataset()

@ex.command
def evaluate():
    print("Evaluate")


@ex.automain
def main():
    print(ex.config)