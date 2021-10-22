#!/usr/bin/env python3
# Electrum ABC - lightweight eCash client
# Copyright (C) 2021 The Electrum ABC developers
#
# Permission is hereby granted, free of charge, to any person
# obtaining a copy of this software and associated documentation files
# (the "Software"), to deal in the Software without restriction,
# including without limitation the rights to use, copy, modify, merge,
# publish, distribute, sublicense, and/or sell copies of the Software,
# and to permit persons to whom the Software is furnished to do so,
# subject to the following conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
# MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
# NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS
# BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN
# ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN
# CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
"""
This module provides coin consolidation tools.
"""
import copy
from typing import List, Optional

from . import wallet
from .address import Address
from .bitcoin import TYPE_ADDRESS
from .transaction import Transaction

MAX_STANDARD_TX_SIZE: int = 100_000
"""Maximum size for transactions that nodes are willing to relay/mine.
"""

MAX_TX_SIZE: int = 1_000_000
"""
Maximum allowed size for a transaction in a block.
"""

FEE_PER_BYTE: int = 1


class AddressConsolidator:
    """Consolidate coins for a single address in a wallet."""

    def __init__(
        self,
        address: Address,
        wallet_instance: wallet.Abstract_Wallet,
        include_coinbase: bool = True,
        include_non_coinbase: bool = True,
        include_frozen: bool = False,
        include_slp: bool = False,
        min_value_sats: Optional[int] = None,
        max_value_sats: Optional[int] = None,
    ):
        self._coins = [
            utxo
            for utxo in wallet_instance.get_addr_utxo(address).values()
            if (
                (include_coinbase or not utxo["coinbase"])
                and (include_non_coinbase or utxo["coinbase"])
                and (include_slp or utxo["slp_token"] is None)
                and (include_frozen or not utxo["is_frozen_coin"])
                and (min_value_sats is None or utxo["value"] >= min_value_sats)
                and (max_value_sats is None or utxo["value"] <= max_value_sats)
            )
        ]
        self.wallet = wallet_instance

        # Cache data common to all coins
        self.address = address
        self.txin_type = wallet_instance.get_txin_type(address)
        self.received = {}
        for tx_hash, height in wallet_instance.get_address_history(address):
            l = self.wallet.txo.get(tx_hash, {}).get(address, [])
            for n, v, is_cb in l:
                self.received[tx_hash + f":{n}"] = (height, v, is_cb)

        if isinstance(self.wallet, wallet.ImportedAddressWallet):
            sig_info = {
                "x_pubkeys": ["fd" + address.to_script_hex()],
                "signatures": [None],
            }
        elif isinstance(self.wallet, wallet.ImportedPrivkeyWallet):
            pubkey = self.wallet.keystore.address_to_pubkey(address)
            sig_info = {
                "x_pubkeys": [pubkey.to_ui_string()],
                "signatures": [None],
                "num_sig": 1,
            }
        elif isinstance(self.wallet, wallet.Multisig_Wallet):
            derivation = self.wallet.get_address_index(address)
            sig_info = {
                "x_pubkeys": [
                    k.get_xpubkey(*derivation) for k in self.wallet.get_keystores()
                ],
                "signatures": [None] * self.wallet.n,
                "num_sig": self.wallet.m,
                "pubkeys": None,
            }
        else:
            # Default case for wallet.Simple_Deterministic_Wallet and Mock wallet used
            # in test
            derivation = self.wallet.get_address_index(address)
            x_pubkey = self.wallet.keystore.get_xpubkey(*derivation)
            sig_info = {
                "x_pubkeys": [x_pubkey],
                "signatures": [None],
                "num_sig": 1,
            }

        # Add more metadata to coins
        for i, c in enumerate(self._coins):
            self.add_input_info(c, sig_info)

    def get_unsigned_transactions(
        self, output_address: Address, max_tx_size: int = MAX_STANDARD_TX_SIZE
    ) -> List[Transaction]:
        """
        Build as many raw transactions as needed to consolidate the coins.

        :param output_address: Make all transactions send the total amount to this
            address.
        :param max_tx_size: Maximum tx size in bytes. This is what limits the
            number of inputs per transaction.
        :return:
        """
        assert max_tx_size < MAX_TX_SIZE
        placeholder_amount = 200
        transactions = []
        i = 0
        while i < len(self._coins):
            tx_size = 0
            amount = 0
            tx = Transaction(None)
            tx.set_inputs([])
            while tx_size < max_tx_size and i < len(self._coins):
                dummy_tx = Transaction(None)
                dummy_tx.set_inputs(tx.inputs() + [self._coins[i]])
                dummy_tx.set_outputs(
                    [(TYPE_ADDRESS, output_address, placeholder_amount)]
                )
                tx_size = len(dummy_tx.serialize(estimate_size=True)) // 2

                if tx_size < max_tx_size:
                    amount = amount + self._coins[i]["value"]
                    tx.add_inputs([self._coins[i]])
                    tx.set_outputs(
                        [
                            (
                                TYPE_ADDRESS,
                                output_address,
                                amount - tx_size * FEE_PER_BYTE,
                            )
                        ]
                    )
                    i += 1

            transactions.append(tx)
        return transactions

    def add_input_info(self, txin, siginfo: dict):
        """Reimplemented from wallet.add_input_info to optimize for multiple calls
        with same address and same history.
        Caching the transaction history is the most significant optimization,
        as the original function loads the history from disk (text file) for
        every call."""
        txin["type"] = self.txin_type
        item = self.received.get(txin["prevout_hash"] + f":{txin['prevout_n']}")
        tx_height, value, is_cb = item
        txin["value"] = value
        txin.update(copy.deepcopy(siginfo))
