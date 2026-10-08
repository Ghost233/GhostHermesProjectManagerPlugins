"""Immutable reviewed native source contract; no runtime SDK patching."""
import hashlib
from pathlib import Path
from types import MappingProxyType

SDK_REVISION = 'bd0affe5e5f723579df8902852f5d0c47795f355'
SDK_BASE_GIT_TREE = '7fa3584dc9608a0f305fde55dbd46f66f6b42bc9'
SDK_BASE_TREE_DIGEST = '97cfb6a77e4e604a77c455f1e4755f3c33783123675acd6e2e0daf35e780c661'
SDK_BASE_FILES = MappingProxyType({
    'plugins/platforms/feishu/adapter.py': '2dcbaa98e9801c2ac0e10b4cf7436e778f6d38fe98d5a585e995700f80147afc',
    'gateway/platforms/base.py': '8657cab37e6e481a7f7893d975c623a24500a216ccf82a1bdf35e473b04a357b',
    'gateway/authz_mixin.py': '2f5b9507cd7fc3d5d559c005ec32317ec6f4aec5479cf89108c7c9d4ca5cd884',
    'gateway/session_identity.py': '9ad91ff2ae1bbce835b6a44c0926d0bbf56c15c91a81c5d595c16f561a0d4d7c',
    'gateway/run.py': 'd0ea287688b45a6e0b67e0bbb0487cd44dff2f434aab628b7e087c951bbb9a54',
    'gateway/profile_routing.py': 'e399603aa627b2794a5723fcafbf1eac5531617dacd0ab8aa0a2e5fdf70c75f4',
    'gateway/run_adapters.py': 'dfb882f4e90e82b3543b1c994671245ba27ab6fbff585bb8b965fef696ac2514',
    'gateway/platform_registry.py': '40c0e47d43462bf174e058189cbb305d88b42e1cd2ca4c3f16fd42656ac76c9e',
    'gateway/config.py': '93fecbfbfc6f09b710aadbd266668f99281ffa567680bf7f9dedbc124e9fb45e',
    'gateway/session.py': 'd274960c58869d456d0063358a8143562ccdbe9471d996e99841275789c4e998',
    'agent/secret_scope.py': '5f2584dc93be49ef7e4de23f9f92a4abd05aa42aab8fc2fac40f54f9f8b9c664',
    'hermes_cli/plugins.py': '31c99f61f61732557bb84429d014e943d2d51a753b2541ab5de3b196a893bf9a',
    'hermes_bootstrap.py': '89bb25daf729f1342166594517480112aba3e27a62e613d2057e3daba1848efc',
    'hermes_logging.py': '0c05185fe5c9d5c65c0eff75611c9e960b9eb8271a89dc6966022a3c590d2686',
    'gateway/run_startup.py': 'c6f959d546fa2b3b8d6b8b395d076905772e8d23f4248f856d4bdbb3ddc639d3',
    'gateway/shutdown_forensics.py': 'aa8b7020afaadda26742c2715e4fe4b5ea00c2b8695a5f53832933d50fb60b38',
    'gateway/shutdown_watchdog.py': 'e4febf3cc51e7117e3b2f7daf3dd3c85889f5a63956340409ed06b5d7b73a71f',
    'hermes_startup_watchdog.py': 'e06cb9b7cc1f847766d7aa495f78f97ed190103235b88c7c9bdc643e44469daa',
    'agent/agent_runtime_helpers.py': '5a1d8f87f8904fa84f2df724767da055c03567d96c753d304b1e985fbdbd2755',
    'hermes_cli/gateway_launchd.py': '84783849b0a9497a43c0e5e70db79cd530c479508d71e8ca0acf6e152ceb8358',
})
SDK_PRIVACY_PATCH_SHA256 = 'ae80f01ce8c72bfa829a37ec4073b07389965308045dd974ae40bc3a35aaca4b'
SDK_PRIVACY_FILES = MappingProxyType({
    'hermes_bootstrap.py': '4b1bd1330ed64b8906d41fbe971db44217cc91cf5edef89fc96aee97a8bd9a16',
    'hermes_logging.py': '49112587f4c0428c2f553f9eba789c5992db19c313f92343e444340e69c867f2',
    'gateway/run.py': '3ea5e65027768d19c2d54ba235df2444ea577efe7bd5e138e5d81d04857b65d7',
    'gateway/run_startup.py': '8620ef0fe04bdb1b9aebf99658879538e16518345219cd4c93aa5d496406154a',
    'gateway/shutdown_forensics.py': '4fa5796993b9578a65cb1ec74b5fa1c0a44dd9b9234d83e8e031df35e840093e',
    'gateway/shutdown_watchdog.py': '7722f5f3dabfc430eadc46f3288baba455208149254277709e5e41b485916571',
    'hermes_startup_watchdog.py': 'c858175a62a7961b227f23d97434b6a9cad14f4aea42b8b261461b7c0c6515ec',
    'agent/agent_runtime_helpers.py': '9e0e4c17e444fc2c81d53afd8c69414c3c7bdda894b96266d05ed15b1362319e',
    'hermes_cli/gateway_launchd.py': 'f63d6cd83947ffad37fca73c8e587a7be36a928019042de82e0496fe097ab0cc',
    'hermes_native_log_privacy.py': '4064ba396c3a4ffd45dd712252cf709d82547cf3584bfcbd05093aa14762de11',
})


def source_files_match(root, expected):
    """Verify required regular source bytes without disclosing their locations."""
    try:
        root = Path(root).resolve(strict=True)
        for name, digest in expected.items():
            path = root / name
            if any(parent.is_symlink() for parent in (path, *path.parents) if parent != root.parent):
                return False
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                return False
        return True
    except (OSError, ValueError):
        return False
