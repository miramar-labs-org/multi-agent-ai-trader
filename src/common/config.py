from omegaconf import OmegaConf


def load_config(path: str = "config.yaml"):
    return OmegaConf.load(path)
