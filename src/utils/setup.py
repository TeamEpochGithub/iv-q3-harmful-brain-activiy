"""Common functions used at the start of the main scripts train.py, cv.py, and submit.py."""
import concurrent.futures
import itertools
import os
import pickle
import re
from collections.abc import Callable
from enum import Enum
from pathlib import Path
from typing import Any, cast

import numpy as np
import numpy.typing as npt
import pandas as pd
import pyarrow.parquet as pq
import torch
import wandb
from epochalyst.pipeline.ensemble import EnsemblePipeline
from epochalyst.pipeline.model.model import ModelPipeline
from hydra.utils import instantiate
from omegaconf import DictConfig, OmegaConf

from src.logging_utils.logger import logger
from src.typing.typing import XData
from src.utils.replace_list_with_dict import replace_list_with_dict


def setup_pipeline(cfg: DictConfig, *, is_train: bool = True) -> ModelPipeline | EnsemblePipeline:
    """Instantiate the pipeline.

    :param pipeline_cfg: The model pipeline config. Root node should be a ModelPipeline
    :param is_train: Whether the pipeline is used for training
    """
    logger.info("Instantiating the pipeline")

    test_size = -1.0
    if is_train:
        # First looks for test_size in the config, then in the splitter config
        test_size = cfg.get("test_size", -1.0)
        if test_size == -1.0:
            test_size = cfg.get("splitter", {}).get("n_splits", -1.0)

    if "model" in cfg:
        model_cfg = cfg.model
        model_cfg_dict = OmegaConf.to_container(model_cfg, resolve=True)
        if isinstance(model_cfg_dict, dict) and is_train:
            model_cfg_dict = update_model_cfg_test_size(model_cfg_dict, test_size)
        pipeline_cfg = OmegaConf.create(model_cfg_dict)

    elif "ensemble" in cfg:
        ensemble_cfg = cfg.ensemble
        ensemble_cfg_dict = OmegaConf.to_container(ensemble_cfg, resolve=True)
        ensemble_cfg_dict = update_ensemble_cfg_dict(ensemble_cfg_dict, test_size, is_train=is_train)
        pipeline_cfg = OmegaConf.create(ensemble_cfg_dict)
    elif "post_ensemble" in cfg:
        post_ensemble_cfg = cfg.post_ensemble
        post_ensemble_cfg_dict = OmegaConf.to_container(post_ensemble_cfg, resolve=True)
        ensemble_cfg_dict = post_ensemble_cfg_dict.get("steps", {}).get("0", {})  # type: ignore[union-attr]
        post_ensemble_cfg_dict.get("steps", {})["0"] = update_ensemble_cfg_dict(ensemble_cfg_dict, test_size, is_train=is_train)  # type: ignore[union-attr]
        ensemble_cfg_dict["steps"] = list(ensemble_cfg_dict["steps"].values())
        pipeline_cfg = OmegaConf.create(ensemble_cfg_dict)
    else:
        raise ValueError("Neither model nor ensemble specified in config.")

    model_pipeline = instantiate(pipeline_cfg)
    logger.debug(f"Pipeline: \n{model_pipeline}")

    return model_pipeline


def update_ensemble_cfg_dict(ensemble_cfg_dict: Any, test_size: float, *, is_train: bool) -> dict[str, Any]:  # noqa: ANN401
    """Update the ensemble_cfg_dict.

    :param ensemble_cfg_dict: The original ensemble_cfg_dict
    :param test_size: Test size to add to the models
    :param is_train: Boolean whether models are being trained
    """
    if isinstance(ensemble_cfg_dict, dict):
        ensemble_cfg_dict["steps"] = list(ensemble_cfg_dict["steps"].values())
        if is_train:
            for model in ensemble_cfg_dict["steps"]:
                update_model_cfg_test_size(model, test_size)

        return ensemble_cfg_dict

    return {}


def update_model_cfg_test_size(
    cfg: dict[str | bytes | int | Enum | float | bool, Any] | list[Any] | str | None,
    test_size: float = -1.0,
) -> dict[str | bytes | int | Enum | float | bool, Any] | list[Any] | str | None:
    """Update the test size in the model config.

    :param cfg: The model config.
    :param test_size: The test size.

    :return: The updated model config.
    """
    if cfg is None:
        raise ValueError("cfg should not be None")
    if isinstance(cfg, dict):
        for model in cfg["train_sys"]["steps"]:
            if model["_target_"] == "src.modules.training.main_trainer.MainTrainer":
                model["test_split_type"] = test_size
    return cfg


def setup_data(
    metadata_path: str | Path | None,
    eeg_path: str | Path | None,
    spectrogram_path: str | Path | None,
    cache_path: str | Path | None = None,
    *,
    use_test_data: bool = False,
) -> tuple[XData, npt.NDArray[np.float32] | None]:
    """Read the metadata and return the data and target in the proper format.

    :param raw_path: Path to the raw data.
    :return: X and y data for training
    """
    if isinstance(metadata_path, str):
        metadata_path = Path(metadata_path)
    if isinstance(eeg_path, str):
        eeg_path = Path(eeg_path)
    if isinstance(spectrogram_path, str):
        spectrogram_path = Path(spectrogram_path)
    if isinstance(cache_path, str):
        cache_path = Path(cache_path)
    # Check that metadata_path is not None
    if metadata_path is None:
        raise ValueError("metadata_path should not be None")

    # Read the metadata
    metadata = pd.read_csv(metadata_path)

    # Process the metadata (Extract ids, offsets, and labels)
    ids = metadata[["patient_id", "eeg_id", "spectrogram_id"]]
    if use_test_data:
        offsets = pd.DataFrame(np.zeros((metadata.shape[0], 2)), columns=["eeg_label_offset_seconds", "spectrogram_label_offset_seconds"])
        labels_np = None
    else:
        offsets = metadata[["eeg_label_offset_seconds", "spectrogram_label_offset_seconds"]]
        labels = metadata[["seizure_vote", "lpd_vote", "gpd_vote", "lrda_vote", "grda_vote", "other_vote"]]
        labels_np = labels.to_numpy()

    # Get one of the paths that is not None
    if cache_path is not None and not os.path.exists(cache_path):
        os.makedirs(cache_path)

    if eeg_path is not None:
        X_eeg = load_all_eegs(eeg_path, cache_path, ids)
    else:
        X_eeg = None

    if spectrogram_path is not None:
        X_kaggle_spec = load_all_spectrograms(spectrogram_path, cache_path, ids)
    else:
        X_kaggle_spec = None

    X_meta = pd.concat([ids, offsets], axis=1)
    X_shared = {"eeg_freq": 200, "eeg_len_s": 50, "kaggle_spec_freq": 0.5, "kaggle_spec_len_s": 600}

    return XData(eeg=X_eeg, kaggle_spec=X_kaggle_spec, eeg_spec=None, meta=X_meta, shared=X_shared), labels_np


def setup_splitter_data(raw_path: str) -> pd.DataFrame:
    """Read the metadata and return the data and target in the proper format."""
    metadata_path = raw_path + "/train.csv"
    metadata = pd.read_csv(metadata_path)
    metadata["index"] = range(len(metadata))
    # Get the first occurance of each eeg_id
    unique_indices = metadata.groupby("eeg_id").first()["index"]
    # Use the index column from X to index the y data
    # Remove the index column from the meta data
    metadata.pop("index")
    # Use the unique indices to index the meta data
    metadata = metadata.iloc[unique_indices]
    # Now split the metadata into the 3 parts: ids, offsets, and labels
    return metadata[["patient_id", "eeg_id", "spectrogram_id"]]


def setup_label_data(raw_path: Path) -> np.ndarray[Any, Any] | None:
    """Read labels from raw_path for training.

    :param raw_path: Raw_path for location of labels
    :return: Labels for training
    """
    if raw_path is None:
        raise ValueError("raw_path should not be None")
    metadata_path = raw_path / "train.csv"

    # Read the metadata
    metadata = pd.read_csv(metadata_path)

    # Check that columns exist
    label_columns = ["seizure_vote", "lpd_vote", "gpd_vote", "lrda_vote", "grda_vote", "other_vote"]
    if all(column in metadata.columns for column in label_columns):
        labels = metadata[label_columns]
    else:
        raise ValueError(f"Columns missing in metadata.columns: {metadata.columns}, label_columns: {label_columns}")

    if labels is None:
        return None
    return labels.to_numpy()


def load_training_data(
    metadata_path: str | Path | None,
    eeg_path: str | Path | None,
    spectrogram_path: str | Path | None,
    cache_path: str | Path | None,
    *,
    x_cache_exists: bool,
    y_cache_exists: bool,
) -> tuple[XData | None, npt.NDArray[np.float32] | None]:
    """Read the data if required and split it in X, y.

    :param metadata_path: Path to the metadata.
    :param eeg_path: Path to the EEG data.
    :param spectrogram_path: Path to the spectrogram data.
    :param cache_path: Path to the cache.
    :param x_cache_exists: Whether the X cache exists.
    :param y_cache_exists: Whether the y cache exists.
    :return: X and y data for training
    """
    if isinstance(metadata_path, str):
        metadata_path = Path(metadata_path)
    if isinstance(eeg_path, str):
        eeg_path = Path(eeg_path)
    if isinstance(spectrogram_path, str):
        spectrogram_path = Path(spectrogram_path)
    if isinstance(cache_path, str):
        cache_path = Path(cache_path)
    if x_cache_exists and not y_cache_exists:
        # Only read y data
        logger.info("x_sys has an existing cache, only loading in labels")
        X = None
        _, y = setup_data(metadata_path, None, None)
    else:
        X, y = setup_data(metadata_path, eeg_path, spectrogram_path, cache_path)

    return X, y


def load_eeg(eeg_path: Path, eeg_id: int) -> tuple[int, pd.DataFrame]:
    """Load the EEG data from the parquet file.

    :param eeg_path: The path to the EEG data.
    :param eeg_id: The EEG id.
    """
    return eeg_id, pq.read_table(eeg_path / f"{eeg_id}.parquet").to_pandas()


def load_spectrogram(spectrogram_path: Path, spectrogram_id: int) -> tuple[int, npt.NDArray[np.float32]]:
    """Load the spectrogram data from the parquet file.

    :param spectrogram_path: The path to the spectrogram data.
    :param spectrogram_id: The spectrogram id.
    """
    data = pd.read_parquet(spectrogram_path / f"{spectrogram_id}.parquet")
    LL = data.filter(regex="^LL")
    LP = data.filter(regex="^LP")
    RP = data.filter(regex="^RP")
    RL = data.filter(regex="^RL")

    spectrogram = np.stack(
        [
            LL.to_numpy().T,
            LP.to_numpy().T,
            RP.to_numpy().T,
            RL.to_numpy().T,
        ],
    )

    return spectrogram_id, spectrogram


def load_all_eegs(eeg_path: Path, cache_path: Path | None, ids: pd.DataFrame) -> dict[int, pd.DataFrame]:
    """Read the EEG data and return it as a dictionary.

    :param eeg_path: Path to the EEG data.
    :param cache_path: Path to the cache, or None if no cache should be used.
    :param eeg_ids: The EEG ids.
    """
    all_eegs = {}
    # Read the EEG data
    logger.info("Reading the EEG data")
    if cache_path is not None and os.path.exists(cache_path / "eeg_cache.pkl"):
        logger.info(f"Found pickle cache for EEG data at: {cache_path / 'eeg_cache.pkl'}")
        with open(cache_path / "eeg_cache.pkl", "rb") as f:
            all_eegs = pickle.load(f)  # noqa: S301
        logger.info("Loaded pickle cache for EEG data")
    else:
        with concurrent.futures.ProcessPoolExecutor() as executor:
            all_eegs = dict(executor.map(load_eeg, itertools.repeat(eeg_path), ids["eeg_id"].unique()))
            executor.shutdown()
        logger.info("Finished reading the EEG data")

        if cache_path is not None:
            logger.info("Saving pickle cache for EEG data")
            with open(cache_path / "eeg_cache.pkl", "wb") as f:
                pickle.dump(all_eegs, f)
            logger.info(f"Saved pickle cache for EEG data to: {cache_path / 'eeg_cache.pkl'}")

    return all_eegs


def load_all_spectrograms(spectrogram_path: Path, cache_path: Path | None, ids: pd.DataFrame) -> dict[int, torch.Tensor]:
    """Read the spectrogram data and return it as a dictionary.

    :param spectrogram_path: Path to the spectrogram data.
    :param cache_path: Path to the cache, or None if no cache should be used.
    :param spectrogram_ids: The spectrogram ids.
    """
    all_spec = {}
    # Read the spectrogram data
    logger.info("Reading the spectrogram data")
    if cache_path is not None and os.path.exists(cache_path / "spectrogram_cache.pkl"):
        logger.info(f"Found pickle cache for spectrogram data at: {cache_path / 'spectrogram_cache.pkl'}")
        with open(cache_path / "spectrogram_cache.pkl", "rb") as f:
            all_spec = pickle.load(f)  # noqa: S301
        logger.info("Loaded pickle cache for spectrogram data")
    else:
        with concurrent.futures.ProcessPoolExecutor() as executor:
            all_spec = dict(executor.map(load_spectrogram, itertools.repeat(spectrogram_path), ids["spectrogram_id"].unique()))
            executor.shutdown()
        for spectrogram_id in all_spec:
            all_spec[spectrogram_id] = torch.tensor(all_spec[spectrogram_id])
        logger.info("Finished reading the spectrogram data")

        if cache_path is not None:
            logger.info("Saving pickle cache for spectrogram data")
            with open(cache_path / "spectrogram_cache.pkl", "wb") as f:
                pickle.dump(all_spec, f)
            logger.info(f"Saved pickle cache for spectrogram data to: {cache_path / 'spectrogram_cache.pkl'}")

    return all_spec


def setup_wandb(
    cfg: DictConfig,
    job_type: str,
    output_dir: Path,
    name: str | None = None,
    group: str | None = None,
) -> wandb.sdk.wandb_run.Run | wandb.sdk.lib.RunDisabled | None:
    """Initialize Weights & Biases and log the config and code.

    :param cfg: The config object. Created with Hydra or OmegaConf.
    :param job_type: The type of job, e.g. Training, CV, etc.
    :param output_dir: The directory to the Hydra outputs.
    :param name: The name of the run.
    :param group: The namer of the group of the run.
    """
    logger.debug("Initializing Weights & Biases")

    config = OmegaConf.to_container(cfg, resolve=True)
    run = wandb.init(
        config=replace_list_with_dict(config),  # type: ignore[arg-type]
        project="detect-harmful-brain-activity",
        entity="team-epoch-iv",
        name=name,
        group=group,
        job_type=job_type,
        tags=cfg.wandb.tags,
        notes=cfg.wandb.notes,
        settings=wandb.Settings(start_method="thread", code_dir="."),
        dir=output_dir,
        reinit=True,
    )

    if isinstance(run, wandb.sdk.lib.RunDisabled) or run is None:  # Can't be True after wandb.init, but this casts wandb.run to be non-None, which is necessary for MyPy
        raise RuntimeError("Failed to initialize Weights & Biases")

    if cfg.wandb.log_config:
        logger.debug("Uploading config files to Weights & Biases")

        # Get the config file name
        if job_type == "sweep":
            job_type = "cv"
        curr_config = "conf/" + job_type + ".yaml"

        # Get the model file name
        if "model" in cfg:
            model_name = OmegaConf.load(curr_config).defaults[2].model
            model_path = f"conf/model/{model_name}.yaml"
        elif "ensemble" in cfg:
            model_name = OmegaConf.load(curr_config).defaults[2].ensemble
            model_path = f"conf/ensemble/{model_name}.yaml"
        elif "post_ensemble" in cfg:
            model_name = OmegaConf.load(curr_config).defaults[2].post_ensemble
            model_path = f"conf/post_ensemble/{model_name}.yaml"

        # Store the config as an artefact of W&B
        artifact = wandb.Artifact(job_type + "_config", type="config")
        config_path = output_dir / ".hydra/config.yaml"
        artifact.add_file(str(config_path), "config.yaml")
        artifact.add_file(curr_config)
        artifact.add_file(model_path)
        wandb.log_artifact(artifact)

    if cfg.wandb.log_code.enabled:
        logger.debug("Uploading code files to Weights & Biases")

        run.log_code(
            root=".",
            exclude_fn=cast(Callable[[str, str], bool], lambda abs_path, root: re.match(cfg.wandb.log_code.exclude, Path(abs_path).relative_to(root).as_posix()) is not None),
        )

    logger.info("Done initializing Weights & Biases")
    return run
