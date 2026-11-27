"""Raster I/O and transforms that preserve transparent coverage."""

from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
import math
import warnings

import numpy as np
from PIL import Image, ImageCms, ImageOps, UnidentifiedImageError
from scipy import ndimage

from .geometry import Box, Region, center_mesh
from .inputs import InputError


MAX_UPLOAD_BYTES = 14 * 1024 * 1024
MAX_PIXELS = 8_000_000
MAX_SIDE = 4200
MAX_OPERATION_PIXELS = 3_500_000
KNOWN_FORMATS = {"PNG", "JPEG", "WEBP", "TIFF"}


@dataclass(frozen=True)
class Decoded:
    image: Image.Image
    format: str
    source_hash: str
    had_profile: bool
    exif_oriented: bool

    @property
    def width(self):
        return self.image.width

    @property
    def height(self):
        return self.image.height


@dataclass(frozen=True)
class Window:
    rgb: np.ndarray
    weights: np.ndarray
    left: int
    top: int
    geometric_area: float

    @property
    def height(self):
        return self.rgb.shape[0]

    @property
    def width(self):
        return self.rgb.shape[1]

    @property
    def coverage(self):
        return float(self.weights.sum(dtype=np.float64))

    def require_pixels(self, minimum=1):
        if self.coverage < minimum:
            raise InputError("样区内没有足够的有效图像像素")
        return self

    def rgba(self):
        alpha = np.rint(self.weights * 255).astype(np.uint8)
        pixels = np.dstack((self.rgb, alpha))
        pixels[alpha == 0, :3] = 0
        return Image.fromarray(pixels, "RGBA")


def read_upload(stream):
    data = stream.read(MAX_UPLOAD_BYTES + 1)
    if not data:
        raise InputError("请选择有内容的图片")
    if len(data) > MAX_UPLOAD_BYTES:
        raise InputError("单张图片最多 14 MiB", status=413)
    return decode_bytes(data)


def check_dimensions(width, height):
    if width < 4 or height < 4:
        raise InputError("图片每边至少需要 4 像素")
    if width > MAX_SIDE or height > MAX_SIDE or width * height > MAX_PIXELS:
        raise InputError("图片超过单边 4200 像素或总数 800 万像素的限制", status=413)


def decode_bytes(data):
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(data)) as original:
                image_format = original.format
                if image_format not in KNOWN_FORMATS:
                    raise InputError("支持 PNG、JPEG、WebP 和单页 TIFF")
                if getattr(original, "n_frames", 1) != 1:
                    raise InputError("请上传静态单帧图像")
                check_dimensions(*original.size)
                orientation = int(original.getexif().get(274, 1) or 1)
                profile = original.info.get("icc_profile")
                oriented = ImageOps.exif_transpose(original)
                rgba = normalize_colors(oriented, profile)
    except InputError:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise InputError("图片超过可解码尺寸", status=413) from None
    except (OSError, UnidentifiedImageError, TypeError, ValueError, SyntaxError):
        raise InputError("图像内容已损坏或格式不能识别") from None
    return Decoded(rgba, image_format, sha256(data).hexdigest(), bool(profile), orientation != 1)


def normalize_colors(image, profile):
    rgba = image.convert("RGBA")
    if not profile:
        return rgba
    try:
        source = ImageCms.ImageCmsProfile(BytesIO(profile))
        target = ImageCms.createProfile("sRGB")
        source_mode = str(ImageCms.getProfileColorSpace(source)).strip()
        if source_mode in ("RGB", "CMYK", "LAB", "GRAY"):
            modes = {"RGB": "RGB", "CMYK": "CMYK", "LAB": "LAB", "GRAY": "L"}
            converted = ImageCms.profileToProfile(image.convert(modes[source_mode]), source, target, outputMode="RGB")
        else:
            raise InputError("图像的色彩配置文件使用了无法转换的色彩空间")
    except (ImageCms.PyCMSError, OSError, ValueError, AttributeError):
        raise InputError("图像的嵌入色彩配置文件无法转换为 sRGB") from None
    converted.putalpha(rgba.getchannel("A"))
    return converted


def png_bytes(image):
    stream = BytesIO()
    image.save(stream, format="PNG", compress_level=4)
    return stream.getvalue()


def jpeg_bytes(image, *, quality=92, background=(250, 248, 242)):
    stream = BytesIO()
    flatten(image, background).save(stream, format="JPEG", quality=quality, optimize=True)
    return stream.getvalue()


def flatten(image, background=(255, 255, 255)):
    rgba = image.convert("RGBA")
    canvas = Image.new("RGB", rgba.size, background)
    canvas.paste(rgba, mask=rgba.getchannel("A"))
    return canvas


def thumbnail(image, size=360):
    preview = image.copy()
    preview.thumbnail((size, size), Image.Resampling.LANCZOS)
    return preview


def open_normalized(path):
    try:
        with Image.open(path) as image:
            return image.convert("RGBA")
    except (OSError, UnidentifiedImageError):
        raise InputError("归档里的图片无法读取", status=409) from None


def window(image, raw_region=None, *, max_pixels=MAX_OPERATION_PIXELS):
    area = Region.read(raw_region, *image.size)
    left, top, right, bottom = area.bounds.integer_extent(*image.size)
    width, height = right - left, bottom - top
    if width * height > max_pixels:
        raise InputError(f"样区超过 {max_pixels:,} 像素，请缩小后再运行")
    if min(width, height) <= 0:
        raise InputError("样区未覆盖像素")
    pixels = np.asarray(image.crop((left, top, right, bottom)), dtype=np.uint8)
    coverage = area.coverage(left, top, width, height)
    alpha = pixels[:, :, 3].astype(np.float32) / 255.0
    return Window(
        pixels[:, :, :3],
        alpha * coverage,
        left,
        top,
        float(coverage.sum(dtype=np.float64)),
    )


def image_samples(image, xx, yy):
    shape = np.broadcast_shapes(xx.shape, yy.shape)
    valid = (xx >= 0) & (xx <= image.width) & (yy >= 0) & (yy <= image.height)
    if not valid.any():
        return np.zeros((*shape, 3), dtype=np.float32), np.zeros(shape, dtype=np.float32)

    def sampled_extent(values, limit):
        first = int(np.clip(math.floor(float(np.min(values)) - 0.5), 0, limit - 1))
        after = int(np.clip(math.ceil(float(np.max(values)) - 0.5) + 1, first + 1, limit))
        return first, after

    left, right = sampled_extent(xx, image.width)
    top, bottom = sampled_extent(yy, image.height)
    rgba = np.asarray(image.crop((left, top, right, bottom)), dtype=np.float32) / 255
    alpha = rgba[:, :, 3]
    index = [np.broadcast_to(yy - 0.5 - top, shape), np.broadcast_to(xx - 0.5 - left, shape)]
    support = ndimage.map_coordinates(alpha, index, order=1, mode="nearest", prefilter=False) * valid
    channels = []
    for channel in range(3):
        sampled = ndimage.map_coordinates(
            rgba[:, :, channel] * alpha,
            index,
            order=1,
            mode="nearest",
            prefilter=False,
        )
        values = np.divide(sampled, support, out=np.zeros_like(sampled), where=support > 1e-6)
        channels.append(np.minimum(values, 1))
    return np.stack(channels, axis=-1), support


def sampled_image(image, xx, yy):
    rgb, alpha = image_samples(image, xx, yy)
    return Image.fromarray(np.rint(np.dstack((rgb, alpha)) * 255).astype(np.uint8), "RGBA")


def crop_region(image, raw_region, *, max_side=1500, border=0):
    region = Region.read(raw_region, *image.size)
    source = window(image, region.as_json())
    result = source.require_pixels().rgba()
    scale = min(1.0, max_side / max(result.size))
    if scale < 1.0:
        size = tuple(max(1, round(dimension * scale)) for dimension in result.size)
        result = result.resize(size, Image.Resampling.LANCZOS)
    if border:
        framed = Image.new("RGBA", (result.width + 2 * border, result.height + 2 * border))
        framed.alpha_composite(result, (border, border))
        result = framed
    return result, {
        "source_bounds": region.bounds.as_list(),
        "pixels_retained": round(source.coverage, 2),
        "scale": round(scale, 7),
        "output_size": list(result.size),
    }


def rectified(image, transform, x0, y0, x1, y1, width, height):
    if width * height > MAX_OPERATION_PIXELS:
        raise InputError("校直结果超过可输出像素数")
    mx, my = center_mesh(0, 0, width, height)
    x = x0 + mx * (x1 - x0) / width
    y = y0 + my * (y1 - y0) / height
    sx, sy = transform.point(x, y)
    return sampled_image(image, sx, sy)


def rotated_crop(image, center, size, degrees):
    width, height = size
    if width * height > MAX_OPERATION_PIXELS:
        raise InputError("转正裁片超过可输出像素数")
    radians = math.radians(degrees)
    direct, cross = math.cos(radians), math.sin(radians)
    mx, my = center_mesh(0, 0, width, height)
    mx = mx - width / 2
    my = my - height / 2
    cx, cy = center
    xx = cx + direct * mx - cross * my
    yy = cy + cross * mx + direct * my
    return sampled_image(image, xx, yy)


