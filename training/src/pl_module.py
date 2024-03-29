import logging
import os
import sqlite3
from typing import Optional, List

import numpy as np
from transformers import AutoTokenizer
import torch
import zstandard
from pytorch_lightning import LightningDataModule
from pytorch_lightning.utilities.cli import DATAMODULE_REGISTRY
from pytorch_lightning.utilities.types import EVAL_DATALOADERS, TRAIN_DATALOADERS
from sortedcontainers import SortedList
from torch.utils.data import DataLoader, Dataset


class PileRandomIODataset(Dataset):
    """
    Used for generating statistics and RandomIO index.
    """

    def __init__(self, fpaths: List[str], max_seq_len: int, pad_id: int):
        self.fpaths = fpaths
        self.max_seq_len = max_seq_len
        self.pad_id = pad_id
        self.decompressor = None

        self.length = 0
        self.index = []
        self.index_keys = SortedList()
        for i, fpath in enumerate(self.fpaths):
            # Connect to DB and get the rows count in each DB.
            conn = sqlite3.connect(fpath)
            num_rows = conn.execute("SELECT COUNT(*) FROM rows").fetchall()[0][0]
            conn.close()

            # Update the length of the dataset. This will end up storing the length of the dataset across all DBs.
            self.length += num_rows
            # Create data structures that are required to implement random access.
            # Maps the index argument of __getitem__ to the DB path and the key in the DB.
            # Stores the length of each DB in a SortableList data structure.
            self.index.append(fpath)
            self.index_keys.add(self.length)
            print(f"DB {fpath} has {num_rows} rows. Total rows {self.length}")

    def get_decompressor(self):
        if self.decompressor is None:
            self.decompressor = zstandard.ZstdDecompressor()
        return self.decompressor

    def __len__(self):
        return self.length

    def _get_db_and_idx(self, idx):
        # Refer to the blog for more details on this.
        key = self.index_keys.bisect_left(idx + 1)
        fpath = self.index[key]

        key_offset = 0 if key == 0 else self.index_keys[key - 1]
        db_key = idx - key_offset

        return fpath, db_key

    def __getitem__(self, idx):
        try:
            fpath, db_idx = self._get_db_and_idx(idx)
            # Open connection for each call. This is not expensive and does not impact speed. Also, kills potential
            # complexity that can arise from keeping many connections open for a long time.
            conn = sqlite3.connect(fpath)
            dataset, seq, pred_start = conn.execute(
                "SELECT dataset, seq, pred_start FROM rows WHERE id == ?", (db_idx,)
            ).fetchall()[0]
            # Close connection
            conn.close()

            tokens = [
                int(x)
                for x in self.get_decompressor()
                .decompress(seq)
                .decode(encoding="ASCII")
                .split()
            ]

            weights = [0] * (pred_start - 1) + [1] * (len(tokens) - pred_start)
            weights += [0] * (self.max_seq_len - len(weights))

            tokens = tokens + [self.pad_id] * (self.max_seq_len + 1 - len(tokens))

            return np.asarray(tokens), np.asarray(weights), dataset
        except Exception as e:
            logging.error(f"idx: {idx} ")
            raise


@DATAMODULE_REGISTRY
class Pile(LightningDataModule):
    def __init__(
        self,
        max_seq_len: int,
        context_len: int,
        batch_size: int,
        tokenizer_path: str,
        path: str,
    ):
        super(Pile, self).__init__()
        self.tokenizer_path = tokenizer_path
        self.tokenizer = AutoTokenizer.from_pretrained(tokenizer_path)
        # self.tokenizer.load(tokenizer_path)
        self.vocab_size = self.tokenizer.vocab_size()

        self.max_seq_len = max_seq_len
        self.context_len = context_len
        self.batch_size = batch_size
        self.path = path

        self.train_dataset = None
        self.val_dataset = None

        self.save_hyperparameters()

    def setup(self, stage: Optional[str] = None) -> None:
        if stage == "fit":
            train_path = os.path.join(self.path, "train")
            val_path = os.path.join(self.path, "val")

            train_paths = sorted(
                [
                    os.path.join(train_path, x)
                    for x in os.listdir(train_path)
                    if x.endswith("db")
                ]
            )
            self.train_dataset = PileRandomIODataset(
                train_paths, self.max_seq_len, self.tokenizer.pad_token_id
            )

            val_paths = sorted(
                [
                    os.path.join(val_path, x)
                    for x in os.listdir(val_path)
                    if x.endswith("db")
                ]
            )
            self.val_dataset = PileRandomIODataset(
                val_paths, self.max_seq_len, self.tokenizer.pad_token_id
            )

    def train_dataloader(self) -> TRAIN_DATALOADERS:
        return DataLoader(
            self.train_dataset,
            batch_size=self.batch_size,
            num_workers=30,
            drop_last=True,
            shuffle=True,
        )

    def val_dataloader(self) -> EVAL_DATALOADERS:
        return DataLoader(
            self.val_dataset, batch_size=self.batch_size, num_workers=30, drop_last=True
        )

    def predict_dataloader(self) -> EVAL_DATALOADERS:
        raise ValueError("No prediction dataloader implemented")

    def on_after_batch_transfer(self, batch, dataloader_idx):
        batch, weights, dataset = batch
        x, y = batch[:, :-1], batch[:, 1:]
        mask = x != 0
        return x, y, mask.type(torch.uint8), weights, dataset