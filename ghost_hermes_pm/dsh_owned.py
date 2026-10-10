"""Dedicated original DSH Host with the shared native Gateway task adapter."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys

from .dsh import DshRemoteAdapter
from .dsh_owned_transport import NativeOwnedTransport
from .manager import ManagementError


class DshOwnedAdapter(DshRemoteAdapter):
    transport = 'owned_native'

    def __init__(self, *, dsh_home, workspace, runtime_package_root, service_ref, node_bin=None,
                 verifier=None, timeout=10):
        paths = (dsh_home, workspace, runtime_package_root)
        if any(not isinstance(value, str) or not Path(value).is_absolute() or str(Path(value).resolve()) != value for value in paths):
            raise ManagementError('invalid_change', 'Owned DSH requires canonical absolute home, workspace and original runtime paths.')
        if not Path(workspace).is_dir() or Path(dsh_home).is_relative_to(Path(workspace)):
            raise ManagementError('invalid_change', 'The independent DSH home must remain outside its existing local workspace.')
        self.dsh_home, self.workspace, self.runtime_package_root = paths
        self.node_bin = node_bin
        self.configuration_sha256 = hashlib.sha256(json.dumps({
            'mode': 'owned_native', 'dsh_home': dsh_home, 'workspace': workspace,
            'runtime_package_root': runtime_package_root, 'node_bin': node_bin,
            'service_ref': service_ref}, sort_keys=True).encode()).hexdigest()
        carrier = NativeOwnedTransport(dsh_home=dsh_home, workspace=workspace,
            runtime_package_root=runtime_package_root, node_bin=node_bin, timeout=timeout)
        super().__init__(None, service_ref=service_ref, verifier=verifier,
                         timeout=timeout, expected_home=dsh_home, transport=carrier)

    def _connection_identity(self, ready):
        native = self._transport.native_identity
        home_digest = hashlib.sha256(ready['host']['home'].encode()).hexdigest()
        return {'service_ref': self.service_ref, 'service_id': 'dsh-owned-native:' + self.configuration_sha256,
                'generation': self.generation, 'platform': sys.platform, 'engine': 'dsh',
                'transport': 'owned_native', 'client_id': ready['clientId'],
                'host_home_sha256': home_digest, 'configuration_sha256': self.configuration_sha256,
                'owned_pid': native['pid'], 'runtime_version': native['version'],
                'process_coverage': 'unverified', 'verified_at': datetime.now(timezone.utc).isoformat()}

    def _verify_boundary(self, proof, repository, required=()):
        super()._verify_boundary(proof, repository, required)
        if (repository.get('worktree') != self.workspace or proof.get('engine') != 'dsh'
                or proof.get('transport') != 'owned_native'
                or proof.get('configuration_sha256') != self.configuration_sha256):
            raise ManagementError('capability_unverified', 'The owned native generation requires independently admitted exact workspace and configuration evidence.')

    def prepare_new_work(self, *, previous_scope_released=False):
        if previous_scope_released is not True or self._pending_inputs or any(
                intent['status'] in {'intent', 'outcome_unknown'} for intent in self._creation_intents.values()):
            raise ManagementError('capability_unverified', 'The prior owned work and repository must be released and unresolved input reconciled before a new instance.')
        if not self._transport._started and not self._closed:
            return
        self.close()
        self.__init__(dsh_home=self.dsh_home, workspace=self.workspace, runtime_package_root=self.runtime_package_root,
                      service_ref=self.service_ref, node_bin=self.node_bin, verifier=self.verifier, timeout=self.timeout)
