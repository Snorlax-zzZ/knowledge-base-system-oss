"""内置 Embedding 服务（infinity）的安装计划与纯逻辑层。

职责边界（openspec embedded-embedding-service v1.2 §3.2 安装归属定死）：
本模块只做**纯业务 + 计划生成**，绝不执行下载 / 不 spawn 进程 / 不跑 pip——
这些动作全部由壳层（mac-app / windows-app 的 ProcessManager）执行。理由：壳层
天然持有进程能力 + venv 写权限，把"执行"集中到单一 owner，避免进度上报 / 文件
权限 / 路径 / 失败回滚在 kb-api 与壳层两侧打架。

关键约束：kb-api 的 .venv **没有装 torch**（torch 在独立的 embedding-service
venv 里），所以设备检测（torch.cuda.is_available）只能由壳层在 embedding venv
中执行；本模块仅生成"检测命令"并对结果做纯逻辑裁决（resolve_device）。

故本模块刻意不含 `download_model` / `subprocess` / `snapshot_download` 调用（AC27）。
"""
from __future__ import annotations

import re
import socket
from dataclasses import dataclass, field
from pathlib import Path

from app.services.disk_space import require_disk_space

# infinity 默认绑定端口（127.0.0.1，不暴露 0.0.0.0，AC15）。被占时自动 +1 避让。
DEFAULT_EMBEDDING_PORT = 7687
# pending chunk ≥ 此阈值时 reindex 才置 maintenance flag 挡写（202）；
# 小于则后台异步重建、允许边搜边写（v1.2 §4.5 写 API 阈值放行）。
REINDEX_MAINTENANCE_THRESHOLD = 5000
# 下载磁盘预检安全系数：模型大小 × 1.5（留出解压 / 临时文件余量）。
_MODEL_DISK_SAFETY_FACTOR = 1.5


@dataclass(frozen=True)
class ModelSpec:
    """内置可选 embedding 模型的元数据。

    size_bytes / ram_bytes 为近似值，仅用于磁盘预检与 UI 展示，非精确约束。
    """

    model_id: str          # HuggingFace repo id
    display_name: str
    dim: int               # 向量维度（决定切模型是否必然 reindex）
    size_bytes: int        # 模型文件近似总大小（磁盘预检用）
    ram_bytes: int         # 常驻内存近似占用（帮 8GB 设备避坑，UI 展示）
    multilingual: bool     # 是否多语言（中英混合 / 纯中文场景选型参考）


_GB = 1024 ** 3

# 内置可选模型注册表。默认推荐 bge-m3（多语言 + 长文本 + 1024 dim，KB 场景甜蜜点）。
MODEL_REGISTRY: dict[str, ModelSpec] = {
    "bge-m3": ModelSpec(
        model_id="BAAI/bge-m3",
        display_name="BGE-M3（多语言，推荐）",
        dim=1024,
        size_bytes=int(2.3 * _GB),
        ram_bytes=int(1.5 * _GB),
        multilingual=True,
    ),
    "bge-large-zh-v1.5": ModelSpec(
        model_id="BAAI/bge-large-zh-v1.5",
        display_name="BGE-large-zh v1.5（纯中文）",
        dim=1024,
        size_bytes=int(1.3 * _GB),
        ram_bytes=int(0.8 * _GB),
        multilingual=False,
    ),
    "qwen3-embedding-0.6b": ModelSpec(
        model_id="Qwen/Qwen3-Embedding-0.6B",
        display_name="Qwen3-Embedding 0.6B",
        dim=1024,
        size_bytes=int(1.2 * _GB),
        ram_bytes=int(0.8 * _GB),
        multilingual=True,
    ),
}

DEFAULT_MODEL_KEY = "bge-m3"

# 合法推理设备。默认 cpu——infinity-emb 检测到 GPU 会自动用 CUDA，未装 driver
# 的笔记本会直接启动失败，故必须显式传 device（v1.2 §4.7）。
VALID_DEVICES = ("cpu", "cuda", "mps")


class EmbeddingInstallError(RuntimeError):
    """安装计划 / 配置阶段的业务异常（区别于壳层执行期异常）。"""


@dataclass
class InstallPlan:
    """交给壳层执行的安装计划（命令字符串集合）。

    本模块只生成此计划，壳层（ProcessManager）负责实际执行：建 venv → pip 装
    infinity-emb → snapshot_download 下模型 → 起 infinity 进程。
    """

    model_spec: ModelSpec
    venv_dir: str                       # embedding-service/venv 绝对路径
    model_dir: str                      # models/{key} 绝对路径
    device: str
    port: int                           # infinity 绑定端口（壳层探活 /health 用同一个）
    create_venv_cmd: list[str]          # 建独立 venv
    pip_install_cmd: list[str]          # 装 infinity-emb[server,torch] + 升级 pip
    download_args: dict[str, str]       # snapshot_download 参数（壳层据此下载）
    start_cmd: list[str]                # 启动 infinity 子进程
    env: dict[str, str] = field(default_factory=dict)  # 启动 infinity 时必须注入的 env（如 INFINITY_BETTERTRANSFORMER=false）
    device_detect_cmd: list[str] = field(default_factory=list)  # 壳层在 venv 内探测 GPU


def resolve_model(model_key: str) -> ModelSpec:
    """把 UI 传入的 model_key 解析为 ModelSpec，未知 key 抛业务异常。"""
    spec = MODEL_REGISTRY.get(model_key)
    if spec is None:
        raise EmbeddingInstallError(
            f"未知模型 key: {model_key}；可选：{', '.join(MODEL_REGISTRY)}"
        )
    return spec


def resolve_device(configured: str | None, detected_cuda: bool | None = None) -> str:
    """裁决最终推理设备（纯逻辑，不 import torch）。

    优先级：用户显式配置 > 壳层回传的 GPU 探测结果 > cpu 兜底。
    configured 非法值直接报错，避免把脏值塞进 infinity 启动命令。
    """
    if configured:
        if configured not in VALID_DEVICES:
            raise EmbeddingInstallError(
                f"非法 device: {configured}；可选：{', '.join(VALID_DEVICES)}"
            )
        return configured
    if detected_cuda:
        return "cuda"
    return "cpu"


def require_model_disk_space(model_key: str, target_dir: str) -> None:
    """下载前磁盘预检：复用 disk_space.require_disk_space，模型大小 × 1.5。

    不足时抛 InsufficientDiskSpaceError（由 API 层转 HTTP 507）。
    """
    spec = resolve_model(model_key)
    require_disk_space(
        target_dir=target_dir,
        required_bytes=int(spec.size_bytes * _MODEL_DISK_SAFETY_FACTOR),
    )


def find_free_port(start_port: int = DEFAULT_EMBEDDING_PORT, host: str = "127.0.0.1",
                   max_tries: int = 64) -> int:
    """从 start_port 起探测第一个空闲端口（bind 测试，纯 IO 无 torch 依赖）。

    注意 TOCTOU：本函数只保证调用瞬间空闲，壳层真正启动 infinity 前应再次确认；
    最终监听端口以壳层写入 runtime/port 的实际值为准。

    历史踩坑：v1.3.12 之前带 ``SO_REUSEADDR=1``——Mac/Linux 上仅允许 TIME_WAIT
    端口复用，行为正常；但 Windows 上 ``SO_REUSEADDR`` 允许两个 LISTENING 共占
    同端口，导致 bind 永远成功 → 撞已占用端口时假阳性返回，调用方实际起 infinity
    时撞 EADDRINUSE 崩。去掉 setsockopt，跨平台行为统一。
    """
    for offset in range(max_tries):
        port = start_port + offset
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind((host, port))
                return port
            except OSError:
                continue
    raise EmbeddingInstallError(
        f"在 {start_port}~{start_port + max_tries - 1} 未找到空闲端口"
    )


def is_owned_infinity(cmdline: str, port: int, model_id: str) -> bool:
    """owner 凭证判定（纯函数）：cmdline 是否为本应用拉起的目标 infinity（AC25 / §4.4）。

    壳层清残留 / 复用端口时调用：读到某 PID 的 cmdline 后用本函数判断是否"自己人"，
    不匹配一律视为外人进程，只换端口绝不杀（避免误杀用户其他程序）。

    匹配规则：cmdline 同时包含目标 port 与 model_id 的 infinity 启动特征。
    """
    if not cmdline:
        return False
    needle_port = f"--port {port}"
    needle_model = f"--model-id {model_id}"
    return "infinity" in cmdline and needle_port in cmdline and needle_model in cmdline


def should_block_writes_for_reindex(pending_chunk_count: int) -> bool:
    """reindex 是否需要置 maintenance flag 挡写（v1.2 §4.5 阈值放行）。

    ≥ 阈值：大库重建耗时长，置 flag 挡写（写 API 返 202 + Retry-After）。
    < 阈值：小库后台异步重建，允许用户继续边搜边写。
    """
    return pending_chunk_count >= REINDEX_MAINTENANCE_THRESHOLD


DEFAULT_PYTORCH_MIRROR = "https://download.pytorch.org/whl/"
DEFAULT_CUDA_VERSION = "cu124"


def _normalize_pytorch_mirror(mirror: str | None) -> str:
    """空值/格式异常回落默认（强制 https + 尾斜杠）；避免脏 URL 流入 pip。

    只允许 https:// —— 走 http 装 wheel 易被 MITM 替换为恶意 wheel（v1.3.12
    审计 P2 供应链入口）。如用户内网 mirror 只有 http，需改源码 + 接受风险。
    """
    m = (mirror or "").strip()
    if not m or not m.startswith("https://") or not m.endswith("/"):
        return DEFAULT_PYTORCH_MIRROR
    return m


def _normalize_cuda_version(version: str | None) -> str:
    """非 cu\\d+ 格式回落 cu121；统一小写。

    pattern `cu\\d{2,4}` 兼容历史 (cu90/cu100/cu118) + 当前 (cu121/cu124/cu128)
    + 未来可能的 4 位标签（如 cu1310）。
    """
    v = (version or "").strip().lower()
    if not re.fullmatch(r"cu\d{2,4}", v):
        return DEFAULT_CUDA_VERSION
    return v


def _build_pip_inline_script(
    resolved_device: str, pytorch_mirror: str, cuda_version: str,
) -> str:
    """生成 pip_install_cmd 的 -c inline script。

    device == "cuda":
      Step 1   装 infinity-emb 全套（拉 CPU torch wheel + 全依赖）
      Step 1.5 force-reinstall torch torchvision，从 {pytorch_mirror}{cuda_version}/
               拉 CUDA wheel 覆盖 Step 1 装的 CPU wheel
      Step 2   force-upgrade numpy>=2.1 + click<8.2 (Python 3.13 + Win 必需 pin)

    device == "cpu" / "mps":
      跳过 Step 1.5；Step 1 装的 CPU wheel 直接用（mac 上自带 mps backend，
      linux/win cpu 用户原生 CPU wheel 就够）。

    URL 安全：pytorch_mirror + cuda_version 在调用前已 normalize 过，这里 repr()
    保证 inline script 内字符串字面量正确（不会有引号 / 反斜杠注入风险）。
    """
    cuda_step = ""
    if resolved_device == "cuda":
        # 用 ``--index-url`` 替换默认 PyPI 索引（PyTorch 官方布局是标准 PEP 503
        # simple index，``{mirror}{cuda_version}`` 即可让 pip 自动拼 ``/torch/``
        # ``/torchvision/`` 找到 CUDA wheel）。
        #
        # 历史踩坑（v1.3.12 第二轮）：之前默认 mirror 用阿里 ``mirrors.aliyun.com
        # /pytorch-wheels/cuXXX/``，实测是阿里云镜像门户 HTML 页面（含 React/JS/广告
        # /SEO 标签），**不是 PEP 503 索引**，pip ``-f`` find-links 模式扒不出 wheel
        # 链接。改默认走 PyTorch 官方（标准 PEP 503）；国内用户需配代理或在设置
        # 页改 mirror 字段指向自建源（必须 PEP 503 兼容）。
        #
        # 同时 cuda_version 默认从 cu121 改 cu124：PyTorch 官方 cu121 没有 cp313 +
        # win_amd64 wheel（cp313 是 Python 3.13 标签），cu124 才有。
        #
        # --force-reinstall：强制替换 Step 1 拉的 CPU wheel；不加的话 pip 看到
        # 已装 CPU torch 版本号 ≥ cuda 源里的，就 skip 不动。
        # --no-deps：避免重新拉 numpy / sympy / typing-extensions 等已装依赖
        # （Step 2 单独 pin）；torchvision 跟 torch 必须从同 index 拉避免错配。
        cuda_url = f"{pytorch_mirror}{cuda_version}"
        cuda_step = (
            "# Step 1.5（cuda only）：force-reinstall torch/torchvision 为 CUDA wheel\n"
            "r = subprocess.call([PY, '-m', 'pip', 'install', '--upgrade',"
            " '--force-reinstall', '--no-deps',"
            f" '--index-url', {cuda_url!r}, 'torch', 'torchvision'])\n"
            "if r != 0:\n"
            "    sys.exit(r)\n"
        )
    return (
        "import subprocess, sys\n"
        "MIRROR = ['--index-url', 'https://pypi.tuna.tsinghua.edu.cn/simple/',"
        " '--extra-index-url', 'https://pypi.org/simple/']\n"
        "PY = sys.executable\n"
        # Step 1：装 infinity-emb 全套依赖（pip 自己拉 numpy 1.26 / click 8.4 +
        # CPU torch wheel；CUDA 用户在 Step 1.5 替换）
        "r = subprocess.call([PY, '-m', 'pip', 'install', *MIRROR,"
        " 'infinity-emb[server,torch]', 'huggingface_hub<1.0'])\n"
        "if r != 0:\n"
        "    sys.exit(r)\n"
        + cuda_step +
        # Step 2：force upgrade Python 3.13 + Win 兼容必需 pin
        # pip 第 N 次只看新装包之间约束，不再考虑已装 infinity-emb metadata，
        # 所以能装上 numpy 2.5 / click 8.1（只 warning 不 fail）
        #
        # 2026-07-02 numpy pin 条件化 + P1-3 收紧（v2）：
        # - Python >=3.10：强升 numpy>=2.1 修 Python 3.13 longdouble 兼容坑
        #   （memory: project_kb_python313_compat.md）+ pin click<8.2 修 typer 兼容
        # - Python <3.10（macOS 系统 3.9）：**完全不动 numpy**。infinity-emb 0.0.77
        #   metadata 硬约束 `numpy<2`，Step 1 拉的 1.26.x 天然符合；py3.9 上
        #   numpy 天花板 2.0.2 且 --upgrade 会尝试升到 2.0.2 破坏 metadata。
        #   只 pin click<8.2 兜住 typer 兼容（跟 py 版本无关）。
        "if sys.version_info >= (3, 10):\n"
        "    r = subprocess.call([PY, '-m', 'pip', 'install', '--upgrade', *MIRROR,"
        " 'numpy>=2.1,<2.3', 'click<8.2'])\n"
        "else:\n"
        "    r = subprocess.call([PY, '-m', 'pip', 'install', '--upgrade', *MIRROR,"
        " 'click<8.2'])\n"
        "sys.exit(r)\n"
    )


def build_install_plan(
    model_key: str,
    data_root: str,
    *,
    device: str | None = None,
    detected_cuda: bool | None = None,
    mirror: str | None = "https://hf-mirror.com",
    pytorch_mirror: str | None = None,
    cuda_version: str | None = None,
) -> InstallPlan:
    """生成交给壳层执行的安装计划（不执行任何下载 / 进程动作）。

    data_root 下布局：embedding-service/venv、models/{key}（与 design §3.1 一致）。
    device 留空时按 resolve_device 裁决（壳层探测结果 / cpu 兜底）。
    pytorch_mirror + cuda_version：仅 device=cuda 时影响 pip 命令（cpu / mps 忽略），
    拼成 ``{pytorch_mirror}{cuda_version}/`` 作为 find-links 源。默认阿里 + cu121
    覆盖 95% 现役 NVIDIA GPU；海外用户可改 https://download.pytorch.org/whl/，
    老驱动（<530）用户改 cu118。
    """
    spec = resolve_model(model_key)
    resolved_device = resolve_device(device, detected_cuda=detected_cuda)
    pytorch_mirror = _normalize_pytorch_mirror(pytorch_mirror)
    cuda_version = _normalize_cuda_version(cuda_version)

    root = Path(data_root)
    venv_dir = root / "embedding-service" / "venv"
    model_dir = root / "models" / model_key

    # 壳层平台差异（venv/bin vs venv/Scripts）由壳层按自身平台拼接；此处给出
    # 逻辑入口名，壳层负责映射到 bin/python 或 Scripts/python.exe。
    venv_python = str(venv_dir / "bin" / "python")
    venv_pip = str(venv_dir / "bin" / "pip")
    venv_infinity = str(venv_dir / "bin" / "infinity_emb")

    return InstallPlan(
        model_spec=spec,
        venv_dir=str(venv_dir),
        model_dir=str(model_dir),
        device=resolved_device,
        port=DEFAULT_EMBEDDING_PORT,
        create_venv_cmd=["python", "-m", "venv", str(venv_dir)],
        # 装 [server,torch]（双 extras 实测够用，全套踩坑见下）：
        #   [server]   v2 启动需要的 FastAPI + uvicorn
        #   [torch]    torch / sentence-transformers（infinity-emb 0.0.77 主依赖
        #              只有 numpy + huggingface_hub，torch 是 optional）
        # 不装 [optimum]：pip 21 装 [optimum] backtrack 45 分钟 + optimum 2.0
        #   移除 bettertransformer + optimum 1.x 又跟 transformers 4.49+ 不兼容
        #   = 版本地狱。改用 env INFINITY_BETTERTRANSFORMER=false 关掉
        #   BetterTransformer 探测（acceleration.py:36 第一行直接 return False，
        #   根本不走 optimum 代码）—— env 在 plan.env 里下发给壳层 StartHandler。
        # 避开 [all]：vision/ct2/audio/tensorrt/onnxruntime-gpu 全拉触发
        #   pip resolver backtrack 几十分钟（1.3.5 实测踩过）。
        # huggingface_hub<1.0：infinity-emb 代码 `from huggingface_hub import
        #   HfFolder`，hf_hub 1.0+ 移除该 API。pin 避开 ImportError。
        #
        # 改 venv_python -c "<script>" 不再 /bin/sh -c：
        #   - Windows 没 /bin/sh，sh-c 直接 FileNotFoundError；改用 venv 自带
        #     python 跑内联 script 跨平台一致（Mac venv/bin/python + Win
        #     venv/Scripts/python.exe 由壳层路径翻译）。
        #   - script 内 subprocess.call 串两步：先装 infinity-emb 全套(让 pip
        #     自己拉依赖,含 numpy 1.26 + click 8.4),后 force-upgrade
        #     numpy>=2.1 + click<8.2(Python 3.13 + Win 必需 pin,见下)。
        #   - 不能一条 pip install 同时指定:infinity-emb 0.0.77 metadata 锁
        #     numpy<2,pip resolver 会拒;但 0.0.77 runtime 跟 numpy 2.5 实测
        #     兼容(import + serving 验证过),拆两步绕开 resolver 强校验。
        # 依赖 pin 必须性：
        #   - numpy>=2.1：numpy 1.x 在 Python 3.13 Win 上 longdouble 初始化撞
        #     OverflowError("cannot convert longdouble infinity to integer"),
        #     numpy 2.1 首个支持 Py 3.13。Mac 3.11/3.12 当前不撞,3.13 升上来必撞。
        #   - click<8.2：click 8.2+ Parameter 校验改了,典 typer 0.12 调用方式
        #     撞 TypeError("Secondary flag is not valid for non-boolean flag"),
        #     infinity-emb CLI 启动直接崩。Mac 3.11/3.12 拉到的 click 通常是 8.1.x
        #     不撞,3.13 强制最新 click 8.4 必撞。
        # 国内镜像：清华源主+PyPI 兜底（torch 123MB PyPI 直连国内常断流；清华
        #   5-10 MB/s 30 秒下完。海外用户访问清华源 CDN 也通，PyPI 留 extra
        #   防清华偶发缺包；不影响功能）。
        pip_install_cmd=[
            venv_python, "-c",
            _build_pip_inline_script(resolved_device, pytorch_mirror, cuda_version),
        ],
        download_args={
            "repo_id": spec.model_id,
            "local_dir": str(model_dir),
            "endpoint": mirror or "",
        },
        # infinity-emb v2 启动模板；壳层实施时按实际 infinity-emb 版本核对参数名。
        # --port 显式传 DEFAULT_EMBEDDING_PORT（7687），不然 infinity 用自己默认
        # 7997 → Swift 端按 plan port 探活 /health 永远撞不到，warmup 必 timeout。
        start_cmd=[
            venv_infinity, "v2",
            "--model-id", str(model_dir),
            "--host", "127.0.0.1",
            "--port", str(DEFAULT_EMBEDDING_PORT),
            "--device", resolved_device,
            "--model-warmup",
        ],
        # INFINITY_BETTERTRANSFORMER=false 关掉 BetterTransformer 探测，绕开
        # acceleration.py:46 引用未定义的 BetterTransformerManager 的 NameError
        # （详见 pip_install_cmd 注释）。
        env={"INFINITY_BETTERTRANSFORMER": "false"},
        device_detect_cmd=[
            venv_python, "-c",
            "import torch; print(torch.cuda.is_available())",
        ],
    )
