"""KLDiv scorer class."""
import logging
import os
import warnings
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import seaborn as sns
import sklearn.metrics
import torch
from matplotlib import pyplot as plt
from sklearn.metrics import confusion_matrix
from torch.nn import KLDivLoss

from src.scoring.scorer import Scorer


class KLDiv(Scorer):
    """Abstract scorer class from which other scorers inherit from."""

    def __init__(self, name: str = "KLDiv", voter_threshold: int | None = None) -> None:
        """Initialize the scorer with a name.

        :param name: The name of the scorer.
        :param voter_threshold: Only include samples with more (exclusive) than voter_threshold voters.
        """
        super().__init__(name)
        self.voter_threshold = voter_threshold

    def __call__(self, y_true: np.ndarray[Any, Any], y_pred: np.ndarray[Any, Any], **kwargs: pd.DataFrame) -> float:
        """Calculate the Kullback-Leibler divergence between two probability distributions.

        :param y_true: The true labels.
        :param y_pred: The predicted labels.
        :return: The Kullback-Leibler divergence between the two probability distributions.
        """
        # Filter out samples with less than voter_threshold voters (sum of y)
        if self.voter_threshold is not None:
            indices = y_true.sum(axis=1) > self.voter_threshold
        else:
            indices = np.ones(y_true.shape[0], dtype=bool)

        # Normalize the true labels to be a probability distribution
        y_true = y_true / y_true.sum(axis=1)[:, None]

        # Get the metadata
        metadata = kwargs.get("metadata", None).reset_index(drop=True)  # type: ignore[union-attr]
        if metadata is None or not isinstance(metadata, pd.DataFrame):
            return -1

        # Filter the metadata
        y_true = y_true[indices]
        y_pred = y_pred[indices]
        metadata = metadata.iloc[indices].reset_index(drop=True)

        grouped_eeg_id = metadata.groupby("eeg_id")
        scores = grouped_eeg_id.apply(lambda group: self.score_group(group, y_true, y_pred))

        return scores.mean()

    def score_group(self, group: pd.DataFrame, y_true: np.ndarray[Any, Any], y_pred: np.ndarray[Any, Any]) -> float:
        """Calculate the Kullback-Leibler divergence between two probability distributions.

        :param group: The group to calculate the score for.
        :param y_true: The true labels.
        :param y_pred: The predicted labels.
        :return: The Kullback-Leibler divergence between the two probability distributions.
        """
        # Get the indices of the current group
        indices = group.index
        # Get the true and predicted labels for the current group
        y_true_group = y_true[indices]
        y_pred_group = y_pred[indices]

        # Calculate the KLDiv for the current group
        return self.calc_kldiv(y_true_group, y_pred_group)

    def calc_kldiv(self, y_true: np.ndarray[Any, Any], y_pred: np.ndarray[Any, Any]) -> float:
        """Calculate the Kullback-Leibler divergence between two probability distributions.

        :param y_true: The true labels.
        :param y_pred: The predicted labels.
        :return: The Kullback-Leibler divergence between the two probability distributions.
        """
        # Convert both to torch tensors
        y_pred = torch.tensor(y_pred)  # type: ignore[assignment]

        target = torch.tensor(y_true)

        # Calculate the KLDivLoss
        criterion = KLDivLoss(reduction="batchmean")
        return criterion(torch.log(torch.clamp(y_pred, min=10**-15, max=1 - 10**-15)), target)  # type: ignore[call-overload]

    def __str__(self) -> str:
        """Return the name of the scorer."""
        return self.name

    def visualize_preds(self, y_true: np.ndarray[Any, Any], y_pred: np.ndarray[Any, Any], output_folder: Path) -> tuple[float, float]:
        """Visualize the predictions.

        :param y_true: The true labels.
        :param y_pred: The predicted labels.
        :param output_folder: The output folder to save the visualization.

        :return: The accuracy and the f1 score of the predictions.
        """
        if self.voter_threshold is not None:
            indices = y_true.sum(axis=1) > self.voter_threshold
        else:
            indices = np.ones(y_true.shape[0], dtype=bool)

        y_true = y_true[indices]
        y_pred = y_pred[indices]

        # Suppress all matplotlib and seaborn warnings
        warnings.filterwarnings("ignore")

        # Get the logger for Matplotlib
        logger = logging.getLogger("matplotlib")

        # Set the level to DEBUG, so INFO messages will be suppressed
        logger.setLevel(logging.WARNING)

        # Create a folder viz if output_folder + viz does not exist
        viz_path = output_folder / "viz_all" if self.voter_threshold is None else output_folder / f"viz_higher_{self.voter_threshold}"
        if not os.path.exists(viz_path):
            os.makedirs(viz_path)

        label_names = np.array(["Seizure", "Lpd", "Gpd", "Lrda", "Grda", "Other"])
        y_pred_final = np.argmax(y_pred, axis=1)
        y_true_final = np.argmax(y_true, axis=1)

        expert_consensus = label_names[y_true_final]
        predict_consensus = label_names[y_pred_final]

        # Correct
        correct = y_pred_final == y_true_final

        # Get the number of voters from y_true
        num_voters = y_true.sum(axis=1)

        # Create a all_df from the y_true and y_pred, correct and num_voters
        all_df = pd.DataFrame(
            {"y_true": y_true_final, "y_pred": y_pred_final, "acc": correct, "num_voters": num_voters, "true_name": expert_consensus, "pred_name": predict_consensus},
        )

        # Create plots for the number of voters

        fig, ax = plt.subplots(2, 1, figsize=(40, 20))

        # Define a palette
        palette = dict(zip(label_names, sns.color_palette("tab10"), strict=False))

        # Create a barplot of correct based on the hue of num_voters
        sns.barplot(data=all_df, x="num_voters", y="acc", ax=ax[0])
        ax[0].set_title("Accuracy based on the number of voters")
        # Make sure the hue is sorted

        sns.countplot(data=all_df, x="num_voters", hue="true_name", ax=ax[1], palette=palette)
        ax[1].set_title("Number of voters based on the true label")
        plt.savefig(viz_path / "num_voters_correct.png")

        # Plot a distribution of the true and predicted labels using seaborn kdeplot
        fig, ax = plt.subplots(2, 1, figsize=(40, 20))

        # Convert the array to a pandas DataFrame
        df_pred = pd.DataFrame(y_pred, columns=label_names)
        # make sure y_true sums to 1
        y_true = y_true / y_true.sum(axis=1)[:, None]
        df_true = pd.DataFrame(y_true, columns=label_names)

        # Melt the DataFrame to long format
        df_pred_long = df_pred.melt(var_name="Label", value_name="Value")
        df_true_long = df_true.melt(var_name="Label", value_name="Value")

        # Plot using sns
        sns.kdeplot(data=df_pred_long, x="Value", hue="Label", fill=True, ax=ax[0])
        sns.kdeplot(data=df_true_long, x="Value", hue="Label", fill=True, ax=ax[1])
        ax[0].set_title("Predicted label distribution")
        ax[1].set_title("True label distribution")
        plt.savefig(viz_path / "label_distribution.png")

        fig, ax = plt.subplots(figsize=(10, 10))

        # Create the confusion matrix
        sns.heatmap(confusion_matrix(y_true_final, y_pred_final), annot=True, fmt="d", ax=ax)
        ax.set_xlabel("Predicted")
        ax.set_ylabel("True")

        # Set the labels
        ax.set_xticklabels(["Seizure", "Lpd", "Gpd", "Lrda", "Grda", "Other"])
        ax.set_yticklabels(["Seizure", "Lpd", "Gpd", "Lrda", "Grda", "Other"])
        ax.set_title("Confusion Matrix")

        # Save the confusion matrix
        plt.savefig(viz_path / "confusion_matrix.png")

        # Calculate the accuracy and the f1 score of the predictions use scikit-learn
        accuracy = sklearn.metrics.accuracy_score(y_true_final, y_pred_final)
        f1 = sklearn.metrics.f1_score(y_true_final, y_pred_final, average="weighted")

        return accuracy, f1
