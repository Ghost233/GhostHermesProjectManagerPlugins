"""Explicit Profile admission; execution and observation proofs remain independent."""
import asyncio
import hashlib
import json
from pathlib import Path

from .manager import ManagementError


def profile_digest(profile):
    return hashlib.sha256(json.dumps({k: profile.get(k) for k in ('id', 'native_profile', 'identity_ref', 'role', 'capability', 'project_id', 'parent_profile_id', 'connection_refs')}, sort_keys=True).encode()).hexdigest()


def enable_profile(manager, profile):
    verifier = getattr(manager.profile_readiness_host, 'verify', None)
    proof = verifier(profile) if callable(verifier) else None
    expected = {'profile_id': profile['id'], 'native_profile': profile['native_profile'], 'identity_ref': profile['identity_ref'], 'configuration_digest': profile_digest(profile), 'status': 'verified'}
    if not isinstance(proof, dict) or any(proof.get(k) != v for k, v in expected.items()) or not proof.get('evidence_ref') or not isinstance(proof.get('channels'), list) or not proof['channels']:
        raise ManagementError('capability_unverified', 'Current native Profile identity and usable channel admission must be independently verified before enablement.')
    profile.update(lifecycle='active', readiness=proof, can_execute=False)


class NativeProfileReadinessHost:
    def __init__(self, intake, profiles, credential_resolver):
        self.intake, self.profiles, self.credential_resolver = intake, profiles, credential_resolver

    def verify(self, profile):
        from .feishu import NativeFeishuTransport
        configuration = self.profiles.get(profile['id'])
        if not isinstance(configuration, dict) or set(configuration) != {'native_home'}:
            raise ManagementError('capability_unverified', 'An explicit original native Profile home is required.')
        home = Path(configuration['native_home'])
        from hermes_cli.profiles import get_profile_dir
        if not home.is_absolute() or home != home.resolve() or home != get_profile_dir(profile['native_profile']).resolve() or not (home / 'config.yaml').is_file():
            raise ManagementError('capability_unverified', 'The existing named native Profile has not been verified.')
        bindings = [b for b in self.intake.settings.get('bindings', []) if b.get('profile_id') == profile['id'] and b.get('project_id') == profile.get('project_id')]
        secret = self.credential_resolver(profile.get('connection_refs', {}).get('credential'))
        digest = profile_digest(profile)
        channels = []
        for binding in bindings:
            if profile.get('connection_refs', {}).get('bot') != 'identity:' + binding['app_id'] + ':' + binding['recipient_open_id']:
                raise ManagementError('binding_conflict', 'The directory bot reference differs from the current native identity.')
            transports = [t for _, t in self.intake.transports if isinstance(t, NativeFeishuTransport) and t.native.config.app_id == binding['app_id']]
            if len(transports) != 1 or not secret or transports[0].native.config.app_secret != secret:
                raise ManagementError('capability_unverified', 'The exact native credential ownership is unverified.')
            locator = self.intake.settings.get('channel_acceptance', {}).get(digest, {}).get(binding['chat_id'])
            channels.append(asyncio.run(transports[0].verify_channel(binding, locator, digest)))
        return {'status': 'verified', 'profile_id': profile['id'], 'native_profile': profile['native_profile'], 'identity_ref': profile['identity_ref'], 'configuration_digest': digest, 'channels': channels, 'evidence_ref': 'native-profile-and-feishu-current-platform-reads'}


def configured_readiness_host(config, intake, credential_resolver):
    if not isinstance(config, dict):
        raise ManagementError('invalid_change', 'Native readiness requires explicit named Profile homes.')
    return NativeProfileReadinessHost(intake, config, credential_resolver)
