import os
import sys
import random
from os.path import join as opj
import torch
import torch.backends.cudnn as cudnn
import torch.distributed as dist
import torch.multiprocessing as mp
sys.path.append(
    os.path.abspath(
        opj(__file__, "../../src")
    )
)
from gedml.launcher.misc import ParserWithConvert
from gedml.launcher.creators import ConfigHandler
from gedml.launcher.misc import utils
from torchdistlog import logging

def main():
    # argparser
    csv_path = os.path.abspath(opj(__file__, "../config/args.csv"))
    parser = ParserWithConvert(csv_path=csv_path, name="AVSL")
    opt, convert_dict = parser.render()

    # args postprocess
    opt.save_path = opj(opt.save_path, opt.save_name)
    if opt.is_resume:
        opt.delete_old = False

    if opt.seed is not None:
        random.seed(opt.seed)
        torch.manual_seed(opt.seed)
    cudnn.deterministic = opt.not_deterministic
    cudnn.benchmark = opt.not_benchmark

    opt.ngpus_per_node = torch.cuda.device_count()
    if opt.is_distributed:
        assert getattr(opt, "machine_size", False)
        opt.world_size = opt.machine_size * opt.ngpus_per_node
        mp.spawn(main_worker, nprocs=opt.ngpus_per_node, args=(convert_dict, opt))
    else:
        main_worker(opt.device, convert_dict, opt)

def main_worker(device, convert_dict, opt):
    # config logger
    logging.getLogger().setLevel(logging.INFO)
    logging.set_dist(is_dist=opt.is_distributed)

    opt.device = device
    if opt.is_distributed:
        # initiate torch.distributed
        opt.rank = opt.ngpus_per_node * opt.machine_rank + int(device)
        dist.init_process_group(
            backend=opt.dist_backend,
            init_method=opt.dist_url,
            world_size=opt.world_size,
            rank=opt.rank
        )
    logging.info("Logging: GPU: {}".format(device))
    print("Print: GPU: {}".format(device))
    
    # initiate objects
    ## hyper-parameters
    start_epoch = 0
    
    if opt.is_distributed:
        ## split in each process
        assert (opt.batch_size % opt.world_size) == 0, \
            "batch-size must be an integer multiple of world-size"
        assert (opt.num_workers % opt.world_size) == 0, \
            "num-workers must be an integer multiple of world-size"
        opt.batch_size = int(opt.batch_size / opt.world_size) # only for trainer
        opt.num_workers = int(opt.num_workers / opt.world_size) # only for trainer

    ## get config-handler
    config_root = os.path.abspath(opj(__file__, "../config/"))
    opt.link_path = opj(config_root, "links", "link_{}.yaml".format(opt.setting))
    opt.assert_path = opj(config_root, "assert.yaml")
    opt.param_path = opj(config_root, "param")

    config_handler = ConfigHandler(
        convert_dict=convert_dict,
        link_path=opt.link_path,
        assert_path=opt.assert_path,
        params_path=opt.param_path,
        is_confirm_first=False
    )

    # register modules
    from avsl import models, collectors, testers, losses
    config_handler.register_packages("models", models)
    config_handler.register_packages("collectors", collectors)
    config_handler.register_packages("testers", testers)
    config_handler.register_packages("losses", losses)

    ## initiate params_dict
    modify_link_dict={
        "datasets": [
            {"train": "{}_train.yaml".format(opt.dataset)},
            {"test": "{}_test.yaml".format(opt.dataset)}
        ]
    }
    if "avsl" in opt.setting:
        modify_link_dict["models"] = [
            {"trunk": "{}_decom.yaml".format(opt.model)},
            {"embedder": "{}_mlp_avsl.yaml".format(opt.model)}
        ]
    else:
        modify_link_dict["models"] = [
            {"trunk": "{}.yaml".format(opt.model)},
            {"embedder": "{}_mlp.yaml".format(opt.model)}
        ]
    params_dict = config_handler.get_params_dict(modify_link_dict=modify_link_dict)

    # modify parameters
    objects_dict = config_handler.create_all(change_dict=opt.__dict__)

    # get manager and start
    manager = utils.get_default(objects_dict, "managers")
    manager.run(
        phase=opt.phase,
        start_epoch=start_epoch,
        is_test=opt.not_test,
        is_save=opt.not_save,
        interval=opt.interval,
        warm_up=opt.warm_up,
        warm_up_list=opt.warm_up_list
    )

if __name__ == "__main__":
    main()
