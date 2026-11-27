"""User-facing input validation and error responses."""

from collections.abc import Mapping, Sequence
import math
import re


class InputError(ValueError):
    def __init__(self, message, *, field=None, status=400):
        super().__init__(message)
        self.message = message
        self.field = field
        self.status = status

    def as_dict(self):
        data = {"error": self.message}
        if self.field:
            data["field"] = self.field
        return data


class NotFound(InputError):
    def __init__(self, message):
        super().__init__(message, status=404)


class Conflict(InputError):
    def __init__(self, message):
        super().__init__(message, status=409)


SAFE_FILENAME = re.compile(r"[^a-zA-Z0-9_.-]+")
SAFE_KEY = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,95}$")
HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")


def obj(value, field="请求"):
    if isinstance(value, Mapping):
        return dict(value)
    raise InputError(f"{field}需要是对象", field=field)


def seq(value, field, *, minimum=0, maximum=256):
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        if minimum <= len(value) <= maximum:
            return list(value)
    raise InputError(f"{field}需要包含 {minimum} 至 {maximum} 项", field=field)


def text(value, field, *, limit=160, blank=False):
    if not isinstance(value, str):
        raise InputError(f"{field}需要是文字", field=field)
    value = value.strip()
    if (not blank and not value) or len(value) > limit:
        raise InputError(f"{field}需要在 {limit} 字以内", field=field)
    if any(ord(ch) < 32 and ch not in ("\n", "\t") for ch in value):
        raise InputError(f"{field}含有不支持的控制字符", field=field)
    return value


def filename(value):
    value = str(value or "image")
    value = value.replace("\\", "/").rsplit("/", 1)[-1]
    value = SAFE_FILENAME.sub("_", value).strip("._")
    return value[:90] or "image"


def key(value, field):
    if isinstance(value, str) and SAFE_KEY.fullmatch(value):
        return value
    raise InputError(f"{field}不是有效的内容编号", field=field)


def flag(value, field):
    if isinstance(value, bool):
        return value
    raise InputError(f"{field}需要明确勾选或取消", field=field)


def number(value, field, *, low=None, high=None):
    if isinstance(value, bool):
        raise InputError(f"{field}需要是数值", field=field)
    try:
        result = float(value)
    except (ValueError, TypeError, OverflowError):
        raise InputError(f"{field}需要是数值", field=field) from None
    if not math.isfinite(result):
        raise InputError(f"{field}需要是有限的数值", field=field)
    if low is not None and result < low:
        raise InputError(f"{field}不得小于 {low:g}", field=field)
    if high is not None and result > high:
        raise InputError(f"{field}不得大于 {high:g}", field=field)
    return result


def integer(value, field, *, low=0, high=10000):
    result = number(value, field, low=low, high=high)
    if not result.is_integer():
        raise InputError(f"{field}需要是整数", field=field)
    return int(result)


def choice(value, field, allowed):
    if not isinstance(value, str) or value not in allowed:
        raise InputError(f"{field}只可取 {' / '.join(allowed)}", field=field)
    return value


def coordinate(value, field):
    parts = seq(value, field, minimum=2, maximum=2)
    return (
        number(parts[0], f"{field}横坐标", low=-25000, high=25000),
        number(parts[1], f"{field}纵坐标", low=-25000, high=25000),
    )


def hex_color(value, field):
    if not isinstance(value, str) or not HEX_COLOR.fullmatch(value):
        raise InputError(f"{field}需要是 #RRGGBB 色值", field=field)
    return tuple(int(value[i:i + 2], 16) for i in (1, 3, 5))


def distinct(values, field):
    if len(set(values)) != len(values):
        raise InputError(f"{field}有重复项", field=field)
    return values


def revision(value):
    return integer(value, "版本号", low=1, high=2147483647)


def optional_text(value, field, limit=160):
    if value is None:
        return ""
    return text(value, field, limit=limit, blank=True)


def in_project(resource, firing_id, field="来源"):
    if resource["firing_id"] != firing_id:
        raise InputError(f"{field}需要来自当前窑次", field=field)
    return resource
