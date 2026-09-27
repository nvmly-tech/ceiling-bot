"""Готовит модель GigaAM для сервера: экспорт в ONNX и квантование весов в int8.

Запускается локально, разово (нужно ~1.6 ГБ памяти — на сервере рядом с ботом столько нет),
из deploy_stt.sh:  prepare_model.py v3_e2e_rnnt <папка>

Квантуются только MatMul/Gemm: квантованные свёртки (ConvInteger) onnxruntime на CPU не исполняет.
Итог: ~310 МБ вместо ~850 МБ, пик памяти при распознавании ~0.75 ГБ вместо ~1.3 ГБ, текст тот же.
"""

import hashlib
import shutil
import sys
import tempfile
from pathlib import Path

import gigaam
import omegaconf
import torch
from onnxruntime.quantization import QuantType, quantize_dynamic


def main(name: str, out: Path) -> None:
    model = gigaam.load_model(name, device="cpu")
    tmp_out = out.with_name(out.name + ".tmp")
    shutil.rmtree(tmp_out, ignore_errors=True)
    tmp_out.mkdir(parents=True)
    with tempfile.TemporaryDirectory() as tmp:
        model.to_onnx(dir_path=tmp, dtype=torch.float32)
        for f in sorted(Path(tmp).iterdir()):
            if f.suffix == ".onnx":
                quantize_dynamic(str(f), str(tmp_out / f.name), weight_type=QuantType.QInt8,
                                 op_types_to_quantize=["MatMul", "Gemm"])
            else:
                shutil.copy(f, tmp_out / f.name)
    # Токенизатор кладём рядом: worker.py берёт его из папки модели, а не по пути из yaml.
    cfg = omegaconf.OmegaConf.load(tmp_out / f"{name}.yaml")
    if tokenizer := cfg.get("decoding", {}).get("model_path"):
        shutil.copy(tokenizer, tmp_out / f"{name}_tokenizer.model")
    sums = "".join(f"{hashlib.sha256(f.read_bytes()).hexdigest()}  {f.name}\n"
                   for f in sorted(tmp_out.iterdir()))
    (tmp_out / "SHA256SUMS").write_text(sums)
    shutil.rmtree(out, ignore_errors=True)
    tmp_out.rename(out)
    print(f"модель готова: {out}")


if __name__ == "__main__":
    main(sys.argv[1], Path(sys.argv[2]))
