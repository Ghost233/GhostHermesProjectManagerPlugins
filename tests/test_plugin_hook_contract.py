import asyncio
import inspect

from test_plugin_entry import Context, fixture_native_home, load_entry


def test_registered_gateway_hook_accepts_forward_compatible_keyword_arguments(tmp_path, monkeypatch):
    fixture_native_home(monkeypatch, tmp_path / 'native-home')
    ctx = Context({})
    load_entry().register(ctx)

    dispatch = ctx.hooks['pre_gateway_dispatch']
    assert any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in inspect.signature(dispatch).parameters.values()
    )
    assert asyncio.run(dispatch(event=None, gateway=None, future_dispatch_option=True)) is None
