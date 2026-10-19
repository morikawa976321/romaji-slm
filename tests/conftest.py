import pytest


@pytest.fixture(scope="session")
def torch_device() -> str:
    """torch 版は GPU（cuda）だけで動かす。CPU は ONNX Runtime 版（docs/decisions.md D44）。GPU の無い機械では torch の試験を飛ばす。"""
    torch = pytest.importorskip("torch")
    if not torch.cuda.is_available():
        pytest.skip("torch 版の試験は GPU（cuda）が要る")
    return "cuda"
