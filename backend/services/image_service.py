"""
Image Generation Service — routes to local ComfyUI (default), Gemini, or opt-in xAI Grok Imagine.

- ComfyUI + NoobAI XL: local anime-style generation (default when running locally).
- Gemini 3.1 Flash Image: cloud fallback via Vertex AI when configured.
- xAI Grok Imagine (opt-in via IMAGE_PROVIDER=xai): POST https://api.x.ai/v1/images/generations
  with default model grok-imagine-image-2.0, n=1, resolution=1k, quality=low,
  aspect_ratio=1:1, response_format=b64_json. Never auto-triggered from narrative
  [IMG:] tags or startup scene pre-generation.
"""
import asyncio
import base64
from enum import Enum
import json
import logging
import os
from pathlib import Path
import re
import time
from typing import Optional
import uuid

import httpx

logger = logging.getLogger(__name__)

COMFYUI_URL = os.getenv("COMFYUI_URL", "http://127.0.0.1:8188")
XAI_IMAGES_URL = "https://api.x.ai/v1/images/generations"
DEFAULT_XAI_IMAGE_MODEL = "grok-imagine-image-2.0"
MAX_DECODED_IMAGE_BYTES = 20 * 1024 * 1024

BACKEND_DIR = Path(__file__).parent.parent
GENERATED_DIR = BACKEND_DIR / "uploads" / "generated"
GENERATED_DIR.mkdir(parents=True, exist_ok=True)
SCENES_DIR = BACKEND_DIR / "uploads" / "scenes"
SCENES_DIR.mkdir(parents=True, exist_ok=True)
# Reference images for visual consistency: portraits and backgrounds per character
REFS_DIR = BACKEND_DIR / "uploads" / "refs"
REFS_DIR.mkdir(parents=True, exist_ok=True)

GCP_CREDENTIALS = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "")
GCP_PROJECT = os.getenv("GCP_PROJECT", "pdfconverter-415414")
GCP_LOCATION = os.getenv("GCP_LOCATION", "global")

FRONTEND_PUBLIC = BACKEND_DIR.parent / "frontend" / "public"

# Match [IMG:] and [SCENE:N] tags, including optional surrounding roleplay asterisks (*..*)
IMG_TAG_PATTERN = re.compile(r"\*?\s*\[IMG:\s*(.+?)\]\s*\*?")
SCENE_TAG_PATTERN = re.compile(r"\*?\s*\[SCENE:\s*(\d+)\]\s*\*?")
# Also detect emoji-based image descriptions the LLM sometimes uses instead of [IMG:]
EMOJI_IMG_PATTERN = re.compile(r"[🖼📸🌄🎨]\s*([^\n\[]{15,200})")

_MODERATION_HINT_RE = re.compile(
    r"(moderat|safety|content[_\s-]?policy|policy[_\s-]?violation|blocked|nsfw|unsafe|filtered)",
    re.IGNORECASE,
)


class Provider(str, Enum):
    GEMINI = "gemini"
    COMFYUI = "comfyui"
    XAI = "xai"


class ImageGenerationError(RuntimeError):
    """Non-secret error raised by explicit image generation flows."""

    def __init__(self, detail: str, status_code: int = 502, code: str = "image_error"):
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code
        self.code = code


def get_image_provider_setting() -> str:
    """Return normalized IMAGE_PROVIDER env setting ('auto' when unset)."""
    raw = os.environ.get("IMAGE_PROVIDER", "").strip().lower()
    return raw if raw else "auto"


def _get_validated_xai_image_key() -> str:
    """
    Resolve scoped xAI image server key (XAI_IMAGE_API_KEY, falling back to XAI_API_KEY).
    Never logs or echoes key material.
    """
    key = (
        os.environ.get("XAI_IMAGE_API_KEY", "").strip()
        or os.environ.get("XAI_API_KEY", "").strip()
    )
    if not key:
        raise ImageGenerationError(
            "xAI image API key is not configured (set XAI_IMAGE_API_KEY or XAI_API_KEY).",
            status_code=503,
            code="missing_api_key",
        )
    if any(ch.isspace() for ch in key) or len(key) < 8:
        raise ImageGenerationError(
            "xAI image API key format is invalid.",
            status_code=503,
            code="invalid_api_key",
        )
    if key.lower().startswith(("your_", "<", "changeme", "replace_me", "xxx")):
        raise ImageGenerationError(
            "xAI image API key is set to a placeholder value.",
            status_code=503,
            code="placeholder_api_key",
        )
    return key


def get_xai_image_model() -> str:
    return os.environ.get("XAI_IMAGE_MODEL", "").strip() or DEFAULT_XAI_IMAGE_MODEL


def is_xai_image_selected() -> bool:
    return get_image_provider_setting() == "xai"


def should_auto_generate_reply_images() -> bool:
    """
    Narrative [IMG:] tags must NEVER silently generate paid images when xAI is
    selected. Local ComfyUI default behavior remains unchanged.
    """
    setting = get_image_provider_setting()
    if setting in ("xai", "none", "disabled"):
        return False
    return True


# ── Provider health checks ──

async def _comfyui_available() -> bool:
    comfy_url = os.environ.get("COMFYUI_URL", COMFYUI_URL)
    try:
        async with httpx.AsyncClient(timeout=3.0) as c:
            r = await c.get(f"{comfy_url}/system_stats")
            return r.status_code == 200
    except Exception:
        return False


_gemini_client = None


def _get_gemini_client():
    global _gemini_client
    if _gemini_client is None:
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = GCP_CREDENTIALS
        from google import genai
        _gemini_client = genai.Client(
            vertexai=True, project=GCP_PROJECT, location=GCP_LOCATION,
        )
    return _gemini_client


async def get_image_provider_health() -> dict:
    """Surface image provider status honestly without making any paid probe."""
    setting = get_image_provider_setting()
    if setting == "xai":
        model = get_xai_image_model()
        try:
            _get_validated_xai_image_key()
            return {
                "provider": "xai",
                "status": "configured",
                "healthy": True,
                "model": model,
                "detail": f"xAI image configured ({model}, explicit-only, unprobed)",
            }
        except ImageGenerationError as err:
            return {
                "provider": "xai",
                "status": "misconfigured",
                "healthy": False,
                "model": model,
                "detail": err.detail,
            }
    if setting in ("none", "disabled"):
        return {
            "provider": "none",
            "status": "disabled",
            "healthy": False,
            "model": None,
            "detail": "Image generation disabled via IMAGE_PROVIDER",
        }
    if setting == "comfyui":
        ok = await _comfyui_available()
        return {
            "provider": "comfyui",
            "status": "connected" if ok else "disconnected",
            "healthy": ok,
            "model": "sdxl",
            "detail": None if ok else "ComfyUI unreachable",
        }
    if setting == "gemini":
        has_creds = bool(os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "").strip() or GCP_CREDENTIALS)
        return {
            "provider": "gemini",
            "status": "configured" if has_creds else "misconfigured",
            "healthy": has_creds,
            "model": "gemini-3.1-flash-image-preview",
            "detail": None if has_creds else "GOOGLE_APPLICATION_CREDENTIALS not set",
        }
    if setting != "auto":
        return {
            "provider": setting,
            "status": "invalid_provider",
            "healthy": False,
            "model": None,
            "detail": f"Unsupported IMAGE_PROVIDER '{setting}'",
        }

    # Default "auto": check local ComfyUI (free local check), no paid cloud probe
    if await _comfyui_available():
        return {
            "provider": "comfyui",
            "status": "connected",
            "healthy": True,
            "model": "sdxl",
            "detail": None,
        }
    return {
        "provider": "auto",
        "status": "standby",
        "healthy": False,
        "model": None,
        "detail": "ComfyUI offline (set IMAGE_PROVIDER=xai for Grok Imagine)",
    }


# ── Asset Validation & Storage ──

def _detect_image_format(data: bytes) -> Optional[tuple[str, str]]:
    """Verify magic bytes and return (extension, content_type), or None if invalid."""
    if not data or len(data) < 16 or len(data) > MAX_DECODED_IMAGE_BYTES:
        return None
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png", "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg", "image/jpeg"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp", "image/webp"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "gif", "image/gif"
    return None


def _persist_image_bytes(
    image_bytes: bytes,
    ext: str,
    content_type: str,
    output_dir: Optional[Path] = None,
    filename_prefix: str = "",
) -> str:
    """Persist validated image bytes using existing R2/local uploads storage."""
    save_dir = output_dir or GENERATED_DIR
    save_dir.mkdir(parents=True, exist_ok=True)
    fname = f"{filename_prefix}{uuid.uuid4().hex}.{ext}"

    # Reuse R2 storage when configured (matching api/upload.py), else local /uploads
    from auth.private_beta import private_beta_enabled
    if (output_dir is None or output_dir == GENERATED_DIR) and not private_beta_enabled():
        try:
            from api.upload import _get_r2, R2_BUCKET, R2_PUBLIC_URL
            r2 = _get_r2()
            if r2:
                key = f"uploads/generated/{fname}"
                r2.put_object(
                    Bucket=R2_BUCKET,
                    Key=key,
                    Body=image_bytes,
                    ContentType=content_type,
                    CacheControl="public, max-age=31536000",
                )
                return f"{R2_PUBLIC_URL}/{key}"
        except Exception as e:
            logger.warning("R2 generated-image upload failed (%s), saving locally", type(e).__name__)

    filepath = save_dir / fname
    filepath.write_bytes(image_bytes)
    rel = filepath.relative_to(BACKEND_DIR)
    return f"/{rel.as_posix()}"


# ── Resolve character reference images ──

def _resolve_avatar_bytes(avatar_url: Optional[str]) -> Optional[bytes]:
    """Load the character's avatar image from local filesystem."""
    if not avatar_url:
        return None
    try:
        if avatar_url.startswith("/avatars/"):
            path = FRONTEND_PUBLIC / avatar_url.lstrip("/")
            if path.exists():
                return path.read_bytes()
        elif avatar_url.startswith("/uploads/"):
            path = BACKEND_DIR / avatar_url.lstrip("/")
            if path.exists():
                return path.read_bytes()
    except Exception as e:
        logger.debug(f"Could not load avatar {avatar_url}: {e}")
    return None


def get_char_ref_bytes(character_id: int) -> Optional[bytes]:
    """
    Load the primary character portrait reference image for use as generation reference.
    Tries uploads/refs/{char_id}/char_0.png first, falls back progressively.
    Returns bytes of the best available reference, or None.
    """
    ref_dir = REFS_DIR / str(character_id)
    for candidate in ["char_0.png", "char_0.jpg", "char_1.png", "char_1.jpg"]:
        p = ref_dir / candidate
        if p.exists():
            try:
                return p.read_bytes()
            except Exception as e:
                logger.debug(f"Could not read ref {p}: {e}")
    return None


def get_char_ref_urls(character_id: int) -> dict[str, str]:
    """
    Return all reference image URLs for a character.
    Keys: 'char_0', 'char_1', 'char_2', 'bg_0', 'bg_1', 'bg_2'
    Values: URL path like /uploads/refs/{char_id}/char_0.png
    """
    ref_dir = REFS_DIR / str(character_id)
    result = {}
    if not ref_dir.exists():
        return result
    for f in sorted(ref_dir.iterdir()):
        if f.suffix.lower() in (".png", ".jpg", ".jpeg"):
            key = f.stem
            rel = f.relative_to(BACKEND_DIR)
            result[key] = f"/{rel.as_posix()}"
    return result


# ── xAI Grok Imagine generation (opt-in, explicit-only) ──

def _classify_xai_image_error(resp: httpx.Response, model: str) -> ImageGenerationError:
    """Map xAI image HTTP error responses without exposing upstream bodies or keys."""
    status = resp.status_code
    if status in (401, 403):
        return ImageGenerationError(
            f"xAI image authentication failed (HTTP {status}). Verify server API key permissions.",
            status_code=502,
            code="upstream_auth_failed",
        )
    if status == 404:
        return ImageGenerationError(
            f"xAI image model '{model}' or endpoint not found (HTTP 404).",
            status_code=502,
            code="model_not_found",
        )
    if status == 429:
        return ImageGenerationError(
            "xAI image rate limit or quota exceeded (HTTP 429). Please wait and try again.",
            status_code=429,
            code="rate_limited",
        )
    if status in (400, 422):
        is_moderated = False
        try:
            err_json = resp.json()
            if isinstance(err_json, dict):
                err_obj = err_json.get("error")
                probe_fields = []
                if isinstance(err_obj, dict):
                    probe_fields.extend([
                        str(err_obj.get("code") or ""),
                        str(err_obj.get("type") or ""),
                        str(err_obj.get("message") or ""),
                    ])
                elif isinstance(err_obj, str):
                    probe_fields.append(err_obj)
                probe_fields.append(str(err_json.get("code") or ""))
                if _MODERATION_HINT_RE.search(" ".join(probe_fields)):
                    is_moderated = True
        except Exception:
            pass

        if is_moderated:
            return ImageGenerationError(
                "Illustration blocked by upstream safety moderation. Try a softer scene prompt.",
                status_code=422,
                code="moderation_blocked",
            )
        return ImageGenerationError(
            f"xAI rejected the image generation request (HTTP {status}).",
            status_code=400,
            code="bad_request",
        )
    if status >= 500:
        return ImageGenerationError(
            f"xAI image service is temporarily unavailable (HTTP {status}).",
            status_code=502,
            code="upstream_unavailable",
        )
    return ImageGenerationError(
        f"xAI image generation failed (HTTP {status}).",
        status_code=502,
        code="upstream_error",
    )


async def generate_image_xai(
    prompt: str,
    output_dir: Optional[Path] = None,
    filename_prefix: str = "",
) -> str:
    """
    Generate a single 1k low-quality 1:1 illustration via xAI Grok Imagine.
    Validates base64 image bytes and persists to asset storage.
    Raises ``ImageGenerationError`` on failure without exposing secrets or bodies.
    """
    api_key = _get_validated_xai_image_key()
    model = get_xai_image_model()
    cleaned_prompt = (prompt or "").strip()[:2000]
    if not cleaned_prompt:
        raise ImageGenerationError("Scene prompt is empty.", status_code=400, code="empty_prompt")

    try:
        timeout_secs = float(os.environ.get("XAI_IMAGE_TIMEOUT_SECONDS", "60"))
    except ValueError:
        timeout_secs = 60.0
    timeout_secs = max(5.0, min(timeout_secs, 180.0))

    payload = {
        "model": model,
        "prompt": cleaned_prompt,
        "n": 1,
        "resolution": "1k",
        "quality": "low",
        "aspect_ratio": "1:1",
        "response_format": "b64_json",
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout_secs, connect=min(15.0, timeout_secs))) as client:
            resp = await client.post(
                XAI_IMAGES_URL,
                headers=headers,
                json=payload,
            )
    except httpx.TimeoutException:
        raise ImageGenerationError(
            "Image generation timed out. Please try again.",
            status_code=504,
            code="timeout",
        )
    except httpx.RequestError:
        raise ImageGenerationError(
            "Cannot reach xAI image service. Check network connectivity.",
            status_code=502,
            code="network_error",
        )

    if resp.status_code >= 400:
        raise _classify_xai_image_error(resp, model)

    try:
        body = resp.json()
    except ValueError:
        raise ImageGenerationError(
            "xAI image service returned malformed JSON.",
            status_code=502,
            code="invalid_json",
        )

    if not isinstance(body, dict):
        raise ImageGenerationError(
            "xAI image service returned an unexpected payload.",
            status_code=502,
            code="invalid_payload",
        )
    if body.get("error"):
        raise ImageGenerationError(
            "xAI image service returned an upstream error.",
            status_code=502,
            code="upstream_error",
        )

    items = body.get("data")
    if not isinstance(items, list) or len(items) < 1 or not isinstance(items[0], dict):
        raise ImageGenerationError(
            "xAI image response contained no generated image.",
            status_code=502,
            code="empty_data",
        )

    b64_raw = items[0].get("b64_json")
    if not isinstance(b64_raw, str) or not b64_raw.strip():
        raise ImageGenerationError(
            "xAI image response was missing b64_json data.",
            status_code=502,
            code="missing_b64",
        )

    b64_clean = b64_raw.strip()
    if b64_clean.startswith("data:") and "," in b64_clean:
        b64_clean = b64_clean.split(",", 1)[1].strip()

    try:
        image_bytes = base64.b64decode(b64_clean, validate=True)
    except Exception:
        raise ImageGenerationError(
            "xAI image response contained invalid base64 data.",
            status_code=502,
            code="invalid_base64",
        )

    fmt = _detect_image_format(image_bytes)
    if fmt is None:
        raise ImageGenerationError(
            "xAI image response did not contain a valid image payload.",
            status_code=502,
            code="invalid_image_bytes",
        )

    ext, content_type = fmt
    url = _persist_image_bytes(
        image_bytes=image_bytes,
        ext=ext,
        content_type=content_type,
        output_dir=output_dir,
        filename_prefix=filename_prefix,
    )
    logger.info("xAI image saved (%s, %d KB)", ext, max(1, len(image_bytes) // 1024))
    return url


# ── Gemini 3.1 Flash Image generation ──

async def generate_image_gemini(
    prompt: str,
    reference_image: Optional[bytes] = None,
    output_dir: Optional[Path] = None,
    filename_prefix: str = "",
) -> Optional[str]:
    """Generate an image via Gemini 3.1 Flash Image with optional character reference."""
    try:
        client = _get_gemini_client()
        from google.genai import types

        save_dir = output_dir or GENERATED_DIR

        if reference_image:
            contents = [
                types.Part.from_bytes(data=reference_image, mime_type="image/png"),
                types.Part.from_text(
                    text=f"Generate a new illustration of this EXACT same character "
                    f"(keep their face, hair, eyes, body proportions identical) "
                    f"in the following scene: {prompt}. "
                    f"Style: detailed anime art, vibrant colors, professional quality. "
                    f"No text, no watermark. Keep character consistency with the reference image."
                ),
            ]
        else:
            contents = (
                f"Generate a high-quality anime illustration: {prompt}. "
                "Style: detailed anime art, vibrant colors, professional quality. "
                "No text, no watermark."
            )

        response = await asyncio.to_thread(
            client.models.generate_content,
            model="gemini-3.1-flash-image-preview",
            contents=contents,
            config=types.GenerateContentConfig(
                response_modalities=["IMAGE", "TEXT"],
                temperature=1.0,
            ),
        )

        for part in response.candidates[0].content.parts:
            if part.inline_data and part.inline_data.mime_type.startswith("image/"):
                ext = part.inline_data.mime_type.split("/")[-1]
                if ext == "jpeg":
                    ext = "jpg"
                fname = f"{filename_prefix}{uuid.uuid4().hex[:12]}.{ext}"
                filepath = save_dir / fname
                filepath.write_bytes(part.inline_data.data)
                logger.info(f"Gemini image saved: {filepath}")
                rel = filepath.relative_to(BACKEND_DIR)
                return f"/{rel.as_posix()}"

        logger.warning("Gemini returned no image parts")
        return None
    except Exception as e:
        logger.error(f"Gemini image generation failed: {type(e).__name__}")
        return None


# ── ComfyUI local generation ──

_COMFYUI_NEGATIVE_BASE = (
    "worst quality, low quality, lowres, bad anatomy, bad hands, missing fingers, "
    "extra digits, deformed, blurry, watermark, text, signature, multiple people, "
    "split screen, panel layout, border, frame, multiple views, ugly, censored, mosaic"
)
_COMFYUI_NEGATIVE_NSFW_EXTRA = (
    ", flat chest, small breasts, fully clothed, conservative outfit, formal suit, "
    "modest, covered up, turtleneck, thick clothing"
)
_COMFYUI_NEGATIVE = _COMFYUI_NEGATIVE_BASE


def _build_sdxl_positive(prompt: str, nsfw: bool = False) -> str:
    if nsfw:
        boosters = (
            "masterpiece, best quality, ultra-detailed, 8k, "
            "1girl, solo, "
            "ecchi, fanservice, sexy, alluring, "
        )
    else:
        boosters = (
            "masterpiece, best quality, ultra-detailed, 8k, "
            "1girl, solo, "
        )
    return boosters + prompt


async def generate_image_comfyui(prompt: str, nsfw: bool = False) -> Optional[str]:
    """Generate an image via local ComfyUI, return URL path."""
    comfy_url = os.environ.get("COMFYUI_URL", COMFYUI_URL)
    try:
        checkpoint = await _get_comfyui_checkpoint()
        if not checkpoint:
            return None
        vae = await _get_comfyui_vae()

        positive = _build_sdxl_positive(prompt, nsfw=nsfw)
        negative = _COMFYUI_NEGATIVE_BASE + (_COMFYUI_NEGATIVE_NSFW_EXTRA if nsfw else "")
        seed = int(time.time() * 1000) % (2 ** 32)
        workflow = _build_comfyui_workflow(positive, negative, checkpoint, vae, seed=seed, nsfw=nsfw)

        client_id = uuid.uuid4().hex
        payload = json.dumps({"prompt": workflow, "client_id": client_id})

        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.post(
                f"{comfy_url}/prompt",
                content=payload,
                headers={"Content-Type": "application/json"},
            )
            r.raise_for_status()
            prompt_id = r.json()["prompt_id"]

        filename, subfolder, img_type = await _wait_comfyui_result(prompt_id)
        if not filename:
            return None

        async with httpx.AsyncClient(timeout=30.0) as c:
            params = {"filename": filename, "subfolder": subfolder, "type": img_type}
            r = await c.get(f"{comfy_url}/view", params=params)
            r.raise_for_status()

            local_name = f"{uuid.uuid4().hex[:12]}.png"
            filepath = GENERATED_DIR / local_name
            filepath.write_bytes(r.content)
            logger.info(f"ComfyUI image saved: {filepath}")
            return f"/uploads/generated/{local_name}"

    except Exception as e:
        logger.error(f"ComfyUI image generation failed: {e}")
        return None


async def _get_comfyui_checkpoint() -> Optional[str]:
    comfy_url = os.environ.get("COMFYUI_URL", COMFYUI_URL)
    async with httpx.AsyncClient(timeout=5.0) as c:
        r = await c.get(f"{comfy_url}/object_info/CheckpointLoaderSimple")
        info = r.json()
        ckpts = info["CheckpointLoaderSimple"]["input"]["required"]["ckpt_name"][0]
        for pref in ["NoobAI", "noobai", "illustrious", "pony", "animagine"]:
            for name in ckpts:
                if pref.lower() in name.lower():
                    return name
        return ckpts[0] if ckpts else None


async def _get_comfyui_vae() -> Optional[str]:
    comfy_url = os.environ.get("COMFYUI_URL", COMFYUI_URL)
    try:
        async with httpx.AsyncClient(timeout=5.0) as c:
            r = await c.get(f"{comfy_url}/object_info/VAELoader")
            info = r.json()
            vaes = info["VAELoader"]["input"]["required"]["vae_name"][0]
            for pref in ["sdxl_vae", "sdxl-vae"]:
                for v in vaes:
                    if pref.lower() in v.lower():
                        return v
            return None
    except Exception:
        return None


def _build_comfyui_workflow(positive, negative, checkpoint, vae=None, seed=42, nsfw=False):
    is_vpred = any(x in checkpoint.lower() for x in ["vpred", "v_pred", "v-pred"])
    cfg = 3.5 if is_vpred else 6.5
    width, height = 832, 1216
    base = {
        "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": checkpoint}},
        "3": {"class_type": "EmptyLatentImage", "inputs": {"width": width, "height": height, "batch_size": 1}},
        "4": {"class_type": "CLIPTextEncode", "inputs": {"text": positive, "clip": ["1", 1]}},
        "5": {"class_type": "CLIPTextEncode", "inputs": {"text": negative, "clip": ["1", 1]}},
        "6": {
            "class_type": "KSampler",
            "inputs": {
                "model": ["1", 0], "positive": ["4", 0], "negative": ["5", 0],
                "latent_image": ["3", 0], "seed": seed,
                "steps": 28, "cfg": cfg, "sampler_name": "euler_ancestral",
                "scheduler": "karras", "denoise": 1.0,
            },
        },
    }
    if vae:
        base["2"] = {"class_type": "VAELoader", "inputs": {"vae_name": vae}}
        base["7"] = {"class_type": "VAEDecode", "inputs": {"samples": ["6", 0], "vae": ["2", 0]}}
    else:
        base["7"] = {"class_type": "VAEDecode", "inputs": {"samples": ["6", 0], "vae": ["1", 2]}}
    base["8"] = {"class_type": "SaveImage", "inputs": {"filename_prefix": "synclub", "images": ["7", 0]}}
    return base


async def _wait_comfyui_result(prompt_id: str, timeout: int = 120):
    comfy_url = os.environ.get("COMFYUI_URL", COMFYUI_URL)
    start = time.time()
    while time.time() - start < timeout:
        try:
            async with httpx.AsyncClient(timeout=5.0) as c:
                r = await c.get(f"{comfy_url}/history/{prompt_id}")
                history = r.json()
                if prompt_id in history:
                    outputs = history[prompt_id].get("outputs", {})
                    for node_output in outputs.values():
                        if "images" in node_output:
                            img = node_output["images"][0]
                            return img["filename"], img.get("subfolder", ""), img.get("type", "output")
        except Exception:
            pass
        await asyncio.sleep(2)
    return None, None, None


# ── Public API ──

async def detect_provider() -> Optional[Provider]:
    """
    Resolve active image provider.
    Honors explicit ``IMAGE_PROVIDER`` setting; defaults to existing local-first
    auto-detection (ComfyUI -> Gemini) when unset.
    """
    setting = get_image_provider_setting()
    if setting in ("none", "disabled"):
        return None
    if setting == "xai":
        return Provider.XAI
    if setting == "comfyui":
        return Provider.COMFYUI if await _comfyui_available() else None
    if setting == "gemini":
        return Provider.GEMINI
    if setting != "auto":
        raise ImageGenerationError(
            f"Unsupported IMAGE_PROVIDER '{setting}'. Expected auto, comfyui, gemini, xai, or none.",
            status_code=503,
            code="invalid_provider",
        )

    if await _comfyui_available():
        return Provider.COMFYUI
    try:
        if os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "").strip() or GCP_CREDENTIALS:
            _get_gemini_client()
            return Provider.GEMINI
    except Exception:
        pass
    return None


async def generate_explicit_illustration(
    prompt: str,
    avatar_url: Optional[str] = None,
    character_id: Optional[int] = None,
    nsfw: bool = False,
) -> tuple[str, Provider]:
    """
    Explicit user-triggered scene illustration.
    Routes to the configured provider (including xAI Grok Imagine when opted in)
    and raises ``ImageGenerationError`` with a safe message on failure.
    """
    provider = await detect_provider()
    if provider is None:
        raise ImageGenerationError(
            "No image provider is available. Start local ComfyUI or set IMAGE_PROVIDER=xai.",
            status_code=503,
            code="no_provider",
        )

    if provider == Provider.XAI:
        url = await generate_image_xai(prompt)
        return url, Provider.XAI

    url = await generate_image(
        prompt,
        provider=provider,
        avatar_url=avatar_url,
        character_id=character_id,
        nsfw=nsfw,
    )
    if not url:
        raise ImageGenerationError(
            f"Image generation via {provider.value} failed.",
            status_code=502,
            code="generation_failed",
        )
    return url, provider


async def generate_image(
    prompt: str,
    provider: Optional[Provider] = None,
    avatar_url: Optional[str] = None,
    character_id: Optional[int] = None,
    nsfw: bool = False,
) -> Optional[str]:
    """
    Generate an image from a text prompt with optional character reference.
    Returns the URL path (e.g. /uploads/generated/xxx.png) or None on failure.
    """
    try:
        if provider is None:
            provider = await detect_provider()
    except ImageGenerationError as err:
        logger.warning("Image provider configuration error: %s", err.detail)
        return None

    if provider is None:
        logger.warning("No image generation provider available")
        return None

    if provider == Provider.XAI:
        try:
            return await generate_image_xai(prompt)
        except ImageGenerationError as err:
            logger.warning("xAI image generation failed (%s)", err.code)
            return None

    logger.info(f"Generating image via {provider.value} (nsfw={nsfw}): {prompt[:80]}...")

    ref_bytes: Optional[bytes] = None
    if character_id is not None:
        ref_bytes = get_char_ref_bytes(character_id)
        if ref_bytes:
            logger.info(f"Using char ref portrait for id={character_id} ({len(ref_bytes)//1024}KB)")
    if ref_bytes is None and avatar_url:
        ref_bytes = _resolve_avatar_bytes(avatar_url)
        if ref_bytes:
            logger.info(f"Using avatar fallback: {avatar_url} ({len(ref_bytes)//1024}KB)")

    if provider == Provider.COMFYUI:
        return await generate_image_comfyui(prompt, nsfw=nsfw)
    return await generate_image_gemini(prompt, reference_image=ref_bytes)


def extract_image_tags(text: str) -> list[str]:
    """Extract all [IMG: description] tags AND emoji-based image descriptions from text."""
    tags = IMG_TAG_PATTERN.findall(text)
    emoji_tags = EMOJI_IMG_PATTERN.findall(text)
    for desc in emoji_tags:
        desc = desc.strip().rstrip("。.！!，,")
        if desc and desc not in tags:
            tags.append(desc)
    return tags


def extract_scene_tags(text: str) -> list[int]:
    """Extract all [SCENE: n] tag indices from text."""
    return [int(m) for m in SCENE_TAG_PATTERN.findall(text)]


def strip_image_tags(text: str) -> str:
    """Remove [IMG: ...], [SCENE: n] tags and emoji image descriptions from text."""
    text = IMG_TAG_PATTERN.sub("", text)
    text = SCENE_TAG_PATTERN.sub("", text)
    text = EMOJI_IMG_PATTERN.sub("", text)
    return text.strip()


def replace_image_tags(text: str, tag_to_url: dict[str, str]) -> str:
    """Replace [IMG: desc] tags and emoji descriptions with markdown image syntax."""
    def _replacer(match):
        desc = match.group(1)
        url = tag_to_url.get(desc)
        if url:
            return f"\n![{desc}]({url})\n"
        return match.group(0)

    text = IMG_TAG_PATTERN.sub(_replacer, text)

    def _emoji_replacer(match):
        desc = match.group(1).strip().rstrip("。.！!，,")
        url = tag_to_url.get(desc)
        if url:
            return f"\n![{desc}]({url})\n"
        return match.group(0)

    text = EMOJI_IMG_PATTERN.sub(_emoji_replacer, text)
    return text


def replace_scene_tags(text: str, idx_to_url: dict[int, str]) -> str:
    """Replace [SCENE: n] tags with markdown image syntax."""
    def _replacer(match):
        idx = int(match.group(1))
        url = idx_to_url.get(idx)
        if url:
            return f"\n![scene {idx}]({url})\n"
        return match.group(0)
    return SCENE_TAG_PATTERN.sub(_replacer, text)


# ── Scene pre-generation ──

def get_scene_dir(character_id: int) -> Path:
    d = SCENES_DIR / str(character_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_pregenerated_scenes(character_id: int) -> dict[int, str]:
    """Return {index: url_path} of existing pre-generated scenes."""
    scene_dir = get_scene_dir(character_id)
    scenes = {}
    for f in sorted(scene_dir.glob("scene_*.png")):
        try:
            idx = int(f.stem.split("_")[1])
            rel = f.relative_to(BACKEND_DIR)
            scenes[idx] = f"/{rel.as_posix()}"
        except (ValueError, IndexError):
            continue
    for f in sorted(scene_dir.glob("scene_*.jpg")):
        try:
            idx = int(f.stem.split("_")[1])
            rel = f.relative_to(BACKEND_DIR)
            scenes[idx] = f"/{rel.as_posix()}"
        except (ValueError, IndexError):
            continue
    return scenes


def scenes_ready(character_id: int, expected: int = 5) -> bool:
    return len(get_pregenerated_scenes(character_id)) >= expected
