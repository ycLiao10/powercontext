#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""在本地复现 bge-small-zh-v1.5 的 INT8 量化（ONNX Runtime 动态/静态量化）。

模型仓库本身已经提供了预量化权重（onnx/model_int8.onnx、onnx/model_quantized.onnx），
本脚本的意义在于证明我们能在本机重新生成一个等价的 INT8 模型，用于端侧推理的
体积/精度对照实验。

用法：
    python scripts/quantize.py --input <model.onnx> --output <model_int8_local.onnx>
    python scripts/quantize.py --input ... --output ... --mode static --calib calib.txt
    python scripts/quantize.py --input ... --output ... --per-channel

依赖：onnxruntime（会自动 import onnx，后者是 onnxruntime.quantization 的硬依赖）
      静态量化还需要 numpy。
"""

import argparse
import os
import sys

# 默认校准语料：10 句中文的向量检索/数据库相关句子。
# 静态量化需要真实激活值分布才能确定量化区间，因此提供覆盖常见领域的中文语料。
DEFAULT_CALIB_SENTENCES = [
    "向量检索是基于嵌入向量的相似度搜索技术，广泛应用于语义搜索和推荐系统。",
    "混合检索将向量检索与全文检索结合，可以同时获得语义匹配和关键词精确匹配的能力。",
    "OceanBase 的 seekdb 模式原生支持向量索引，适合构建大规模知识库应用。",
    "在浏览器端运行 embedding 模型，可以让文本向量在本地生成，避免上传原始数据。",
    "量化通过降低模型的数值精度来减少体积并提升推理速度，常见精度有 FP32、FP16 和 INT8。",
    "知识库系统需要支持文档的增删改查，以及基于自然语言的智能问答。",
    "余弦相似度是衡量两个向量之间语义相关程度最常用的度量方式。",
    "本地推理意味着数据不出设备，在隐私敏感的场景中非常重要。",
    "数据库索引可以显著提升查询性能，向量索引则加速了高维向量的近似最近邻搜索。",
    "混合排序通常先召回候选集，再用交叉编码器对结果进行精排以提升相关性。",
]


def _fail(msg, code=1):
    sys.stderr.write("[quantize] 错误：%s\n" % msg)
    sys.exit(code)


def import_quantization():
    """延迟导入 onnxruntime.quantization，使 --help 在未装依赖时也能运行。"""
    try:
        from onnxruntime.quantization import (  # noqa: F401
            quantize_dynamic,
            quantize_static,
            QuantType,
            QuantFormat,
            CalibrationMethod,
            CalibrationDataReader,
        )
    except ImportError as exc:
        missing = getattr(exc, "name", None)
        if missing == "onnx":
            _fail(
                "缺少 onnx 包（onnxruntime.quantization 的必需依赖）。\n"
                "        请执行: pip install onnx\n"
                "        或一次性安装: pip install onnxruntime onnx",
                code=2,
            )
        _fail(
            "缺少 onnxruntime（%s）。\n"
            "        请执行: pip install onnxruntime\n"
            "        静态量化还需要 numpy: pip install numpy" % (missing or exc),
            code=2,
        )


def read_calibration_sentences(calib_path):
    """从校准文件按行读取句子；文件不存在或为空时报错退出。"""
    if not calib_path:
        return list(DEFAULT_CALIB_SENTENCES)
    if not os.path.isfile(calib_path):
        _fail("校准文件不存在：%s" % calib_path, code=2)
    sentences = []
    with open(calib_path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                sentences.append(line)
    if not sentences:
        _fail("校准文件为空：%s" % calib_path, code=2)
    return sentences


def load_vocab(model_dir):
    """读取 tokenizer.json 中的词表；找不到时返回空 dict（退化为近似分词）。"""
    if not model_dir:
        return {}
    tok_path = os.path.join(model_dir, "tokenizer.json")
    if not os.path.isfile(tok_path):
        return {}
    try:
        import json

        with open(tok_path, "r", encoding="utf-8") as fh:
            tok = json.load(fh)
        return tok.get("model", {}).get("vocab", {}) or {}
    except Exception:  # pragma: no cover - 词表解析失败时退化为近似分词
        return {}


def build_calibration_reader(sentences, model_dir):
    """构造 CalibrationDataReader。

    本机没有 transformers/tokenizers，无法直接调用 HF 分词器，这里用一个与
    HF BERT 词表兼容的轻量实现：从 tokenizer.json 读取词表后做字符级映射，
    得到 input_ids / attention_mask / token_type_ids。
    若找不到 tokenizer.json，则退化为按字符取模映射到词表范围的近似分词，
    仅用于给静态量化提供形状正确的激活样本。
    """
    import numpy as np
    from onnxruntime.quantization import CalibrationDataReader

    vocab = load_vocab(model_dir)
    max_len = 128
    unk_id = vocab.get("[UNK]", 100)
    cls_id = vocab.get("[CLS]", 101)
    sep_id = vocab.get("[SEP]", 102)
    vocab_size = (max(vocab.values()) + 1) if vocab else 30522

    def encode(text):
        if not vocab:
            # 近似分词：按字符取模映射到词表范围
            ids = [1 + (ord(ch) % (vocab_size - 1)) for ch in text][: max_len - 2]
        else:
            ids = []
            for ch in text:
                if ch in vocab:
                    ids.append(vocab[ch])
                else:
                    ids.append(unk_id)
            ids = ids[: max_len - 2]
        return [cls_id] + ids + [sep_id]

    class SentenceCalibrationReader(CalibrationDataReader):
        def __init__(self, lines):
            self._lines = lines
            self._iter = None

        def _build(self):
            self._iter = []
            for line in self._lines:
                ids = encode(line)
                input_ids = np.array([ids], dtype=np.int64)
                attention_mask = np.ones_like(input_ids)
                token_type_ids = np.zeros_like(input_ids)
                self._iter.append(
                    {
                        "input_ids": input_ids,
                        "attention_mask": attention_mask,
                        "token_type_ids": token_type_ids,
                    }
                )
            return self._iter

        def get_next(self):
            if self._iter is None:
                self._build()
            if not self._iter:
                return None
            return self._iter.pop(0)

        def rewind(self):
            self._iter = None

    return SentenceCalibrationReader(sentences)


def main():
    parser = argparse.ArgumentParser(
        description="在本地把 FP32 ONNX 权重量化为 INT8（ONNX Runtime 动态/静态量化）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例：\n"
        "  python scripts/quantize.py --input models/Xenova/bge-small-zh-v1.5/onnx/model.onnx \\\n"
        "      --output models/Xenova/bge-small-zh-v1.5/onnx/model_int8_local.onnx\n",
    )
    parser.add_argument("--input", required=True, help="输入的 FP32 ONNX 模型路径")
    parser.add_argument("--output", required=True, help="输出的量化 ONNX 模型路径")
    parser.add_argument(
        "--mode",
        choices=["dynamic", "static"],
        default="dynamic",
        help="量化方式：dynamic（默认，权重动态量化）/ static（需要校准数据）",
    )
    parser.add_argument("--calib", default=None, help="校准文本文件，每行一句；不提供则使用内置中文语料")
    parser.add_argument("--per-channel", action="store_true", help="按通道量化（默认关闭）")
    args = parser.parse_args()

    if not os.path.isfile(args.input):
        _fail("输入模型不存在：%s" % args.input, code=2)

    import_quantization()
    from onnxruntime.quantization import (
        quantize_dynamic,
        quantize_static,
        QuantType,
        QuantFormat,
        CalibrationMethod,
    )

    input_bytes = os.path.getsize(args.input)
    out_dir = os.path.dirname(os.path.abspath(args.output))
    if out_dir and not os.path.isdir(out_dir):
        os.makedirs(out_dir, exist_ok=True)

    cmd = "python %s --input %s --output %s --mode %s" % (
        os.path.basename(__file__),
        args.input,
        args.output,
        args.mode,
    )
    if args.calib:
        cmd += " --calib %s" % args.calib
    if args.per_channel:
        cmd += " --per-channel"

    print("[quantize] 命令：%s" % cmd)
    print("[quantize] 输入：%s（%d 字节，%.2f MB）" % (args.input, input_bytes, input_bytes / 1048576.0))

    if args.mode == "dynamic":
        print("[quantize] 方式：动态量化 weight_type=QUInt8 per_channel=%s reduce_range=False" % args.per_channel)
        quantize_dynamic(
            args.input,
            args.output,
            weight_type=QuantType.QUInt8,
            per_channel=args.per_channel,
            reduce_range=False,
        )
    else:
        sentences = read_calibration_sentences(args.calib)
        # tokenizer.json 与 onnx 同级目录的上级：<modelId>/onnx/model.onnx -> <modelId>/
        model_dir = os.path.dirname(os.path.dirname(os.path.abspath(args.input)))
        print("[quantize] 方式：静态量化 QDQ，校准样本 %d 条，词表目录 %s" % (len(sentences), model_dir))
        reader = build_calibration_reader(sentences, model_dir)
        quantize_static(
            args.input,
            args.output,
            reader,
            quant_format=QuantFormat.QDQ,
            per_channel=args.per_channel,
            activation_type=QuantType.QUInt8,
            weight_type=QuantType.QInt8,
            calibrate_method=CalibrationMethod.MinMax,
        )

    if not os.path.isfile(args.output):
        _fail("量化失败：输出文件未生成：%s" % args.output)

    output_bytes = os.path.getsize(args.output)
    ratio = float(input_bytes) / output_bytes if output_bytes else 0.0
    print("[quantize] 输出：%s（%d 字节，%.2f MB）" % (args.output, output_bytes, output_bytes / 1048576.0))
    print("[quantize] compression_ratio = %d / %d = %.4f" % (input_bytes, output_bytes, ratio))
    print("[quantize] 体积下降 %.2f%%" % ((1.0 - float(output_bytes) / input_bytes) * 100.0))


if __name__ == "__main__":
    main()
