"""Автоматическая диагностика ошибок загрузки (на русском языке)."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .redact import clean


@dataclass
class ErrorInfo:
    code: str
    title: str
    cause: str
    solution: str
    retryable: bool = True
    actions: list[str] = field(default_factory=list)  # подсказки интерфейсу: update_ytdlp, use_firefox, ...
    technical: str = ""

    def to_dict(self) -> dict:
        return {
            "code": self.code, "title": self.title, "cause": self.cause, "solution": self.solution,
            "retryable": self.retryable, "actions": list(self.actions), "technical": self.technical,
        }


# (код, regex, заголовок, причина, решение, можно ли повторить, действия)
_RULES: list[tuple[str, str, str, str, str, bool, list[str]]] = [
    ("EJS_INCOMPAT", r"addOnDisposeCallback is not a function|yt[-_]dlp[-_]ejs.*(incompatible|mismatch|version)",
     "Несовместимость EJS и JavaScript-среды",
     "Версия yt-dlp-ejs не подходит к установленной версии Deno/Node или устарела.",
     "Обновите yt-dlp и Deno в разделе «Компоненты и обновления». Вместе с актуальным yt-dlp приходит совместимый EJS.",
     True, ["update_ytdlp", "update_deno"]),
    ("JS_CHALLENGE", r"n challenge solving failed|signature solving failed|challenge solving failed|\[jsc[^\]]*\].*(fail|error)|Only images are available.*n challenge",
     "Не удалось решить JavaScript-проверку YouTube",
     "YouTube изменил защиту плеера; yt-dlp/EJS устарели или JavaScript-среда (Deno) недоступна.",
     "Обновите yt-dlp, EJS и Deno. При повторной ошибке включите «Загружать EJS с GitHub при необходимости» в разделе компонентов.",
     True, ["update_ytdlp", "update_deno", "enable_remote_ejs"]),
    ("NO_JS_RUNTIME", r"no (supported )?javascript runtime|javascript runtime.*(not found|missing|unavailable)|js[_ ]runtime.*not (found|available)",
     "Не найдена JavaScript-среда (Deno/Node)",
     "Для YouTube нужен Deno или Node.js, но они не найдены или не запускаются.",
     "Установите/обновите Deno в разделе «Компоненты и обновления» и запустите проверку работоспособности.",
     True, ["update_deno"]),
    ("CHROME_DPAPI", r"failed to decrypt with dpapi",
     "Chrome/Edge не отдаёт cookies (ошибка DPAPI)",
     "Современные Chromium-браузеры шифруют cookies способом, который yt-dlp не может прочитать в вашей конфигурации Windows.",
     "Используйте Firefox: войдите в аккаунт в Firefox и выберите его в разделе «Авторизация». Либо импортируйте cookies.txt.",
     False, ["use_firefox"]),
    ("COOKIES_LOCKED", r"could not copy (chrome|chromium|edge|brave|opera|vivaldi)? ?cookie database|cookie database.*(locked|in use|permission)|database is locked.*cookie|permissionerror.*cookies",
     "Файл cookies заблокирован браузером",
     "Браузер запущен и удерживает базу cookies открытой, либо к файлу нет доступа.",
     "Полностью закройте браузер (включая фоновые процессы) и повторите. Либо используйте Firefox или cookies.txt.",
     True, ["use_firefox", "close_browser"]),
    ("AGE_RESTRICTED", r"sign in to confirm your age|confirm your age|age[- ]restricted|inappropriate for some users",
     "Видео с возрастным ограничением",
     "Для просмотра требуется авторизованный аккаунт с подтверждённым возрастом.",
     "Настройте авторизацию (рекомендуется Firefox) в разделе «Авторизация» и убедитесь, что аккаунт имеет доступ к этому видео.",
     True, ["setup_auth"]),
    ("BOT_CHECK", r"sign in to confirm you.?re not a bot|confirm you.?re not a bot",
     "YouTube запрашивает подтверждение, что вы не бот",
     "Запрос выглядит подозрительно для YouTube (часто — без авторизации или с общего IP).",
     "Используйте авторизацию через Firefox, обновите yt-dlp и повторите позже.",
     True, ["setup_auth", "update_ytdlp"]),
    ("AUTH_REQUIRED", r"login required|please log ?in|requires (a )?login|rate-limit reached or login required|cookies are no longer valid|this content is only available for registered|sign in to (view|watch|access)|private account|not logged in|authentication (required|failed)",
     "Требуется авторизация или она истекла",
     "Платформа требует входа в аккаунт, либо сохранённые cookies устарели.",
     "Войдите на платформу в браузере, затем обновите авторизацию в разделе «Авторизация» и нажмите «Проверить авторизацию».",
     True, ["setup_auth"]),
    ("PRIVATE_VIDEO", r"private video|this video is private|video is private",
     "Приватное видео",
     "Автор ограничил доступ к видео.",
     "Если у вас есть доступ, войдите в аккаунт через раздел «Авторизация». Иначе скачать видео нельзя.",
     False, ["setup_auth"]),
    ("GEO_BLOCK", r"not available in your country|geo[- ]?restrict|blocked (it )?in your country|not available from your location|georestriction|uploader has not made this video available in your country",
     "Региональное ограничение",
     "Видео недоступно в вашей стране.",
     "Используйте легальный способ доступа к контенту в вашем регионе. Приложение не обходит региональные ограничения.",
     False, []),
    ("PO_TOKEN", r"po[ _-]?token|gvs po token|missing a url|pot provider",
     "Ошибка PO Token",
     "YouTube требует PO Token для некоторых клиентов/форматов.",
     "Обновите yt-dlp (в новых версиях PO Token обрабатывается автоматически) и используйте авторизацию через Firefox.",
     True, ["update_ytdlp", "setup_auth"]),
    ("HTTP_403", r"http error 403|403: forbidden|forbidden \(403\)",
     "Доступ запрещён (HTTP 403)",
     "Сервер отклонил запрос: устаревший yt-dlp, истёкшая ссылка или блокировка.",
     "Обновите yt-dlp, при необходимости настройте авторизацию и повторите попытку.",
     True, ["update_ytdlp", "setup_auth"]),
    ("HTTP_429", r"http error 429|too many requests|429: ",
     "Слишком много запросов (HTTP 429)",
     "Платформа временно ограничила частоту запросов.",
     "Подождите несколько минут, уменьшите число параллельных загрузок и/или задайте ограничение скорости.",
     True, ["reduce_parallel"]),
    ("IMPERSONATE_UNAVAILABLE", r"impersonat\w*.*(not available|unsupported|not supported|missing|no (impersonate )?target)|no impersonate target is available",
     "Имитация браузера недоступна",
     "В этой сборке yt-dlp нет поддержки impersonation (curl_cffi).",
     "Обновите yt-dlp до официальной сборки. Загрузка будет повторена без имитации.",
     True, ["update_ytdlp"]),
    ("DISK_FULL", r"no space left on device|not enough space|errno 28|enospc|there is not enough space on the disk|disk (is )?full",
     "Недостаточно места на диске",
     "На диске загрузок закончилось свободное место.",
     "Освободите место или выберите другую папку в настройках, затем повторите загрузку.",
     True, ["change_folder"]),
    ("FFPROBE_MISSING", r"ffprobe.*(not found|not installed|unable to find)|unable to find ffprobe|ffprobe and ffmpeg not found",
     "Не найден FFprobe",
     "FFprobe отсутствует или не запускается.",
     "Установите FFmpeg и FFprobe из одной сборки в разделе «Компоненты и обновления».",
     True, ["update_ffmpeg"]),
    ("FFMPEG_ERROR", r"ffmpeg(\.exe)? (is )?not (found|installed)|ffmpeg.*(exited with code|error|failed)|postprocessing: .*error|conversion failed|error merging|invalid data found when processing input|unable to find ffmpeg",
     "Ошибка FFmpeg",
     "FFmpeg не найден, повреждён или не смог объединить/преобразовать дорожки.",
     "Переустановите FFmpeg в разделе «Компоненты и обновления», проверьте свободное место и повторите загрузку.",
     True, ["update_ffmpeg"]),
    ("FORMAT_UNAVAILABLE", r"requested format is not available|requested format not available|no video formats found|only images are available|no formats? (found|available)|the following content is not available on this app",
     "Запрошенный формат недоступен",
     "У видео нет формата с выбранными качеством/кодеком/контейнером.",
     "Выберите «Максимальное» качество и кодек «Автоматически», затем повторите.",
     True, ["relax_format"]),
    ("TIMEOUT", r"timed out|timeout|vds_stall_timeout",
     "Тайм-аут загрузки",
     "Сервер или сеть не отвечали слишком долго.",
     "Проверьте подключение и повторите. При нестабильной сети уменьшите число параллельных загрузок.",
     True, []),
    ("NETWORK", r"getaddrinfo failed|temporary failure in name resolution|connection (reset|aborted|refused|error)|network is unreachable|failed to establish a new connection|remote end closed|urlopen error|unable to download (webpage|json|video data)|transporterror|ssl(error)?:|name or service not known|no route to host|incompleteread",
     "Ошибка сетевого соединения",
     "Нет доступа к сети или соединение прервалось.",
     "Проверьте интернет, VPN/прокси и брандмауэр, затем повторите.",
     True, []),
    ("VIDEO_REMOVED", r"video unavailable|has been removed|this video (has been|is) (removed|deleted|no longer available)|no longer available|account.*terminated|video not found|does not exist|http error 404|404: not found|this video isn.t available|content isn.t available|unavailable video",
     "Видео удалено или недоступно",
     "Видео удалено автором/платформой либо ссылка неверна.",
     "Проверьте ссылку и откройте видео в браузере. Если оно открывается, обновите yt-dlp.",
     False, ["update_ytdlp"]),
    ("OUTDATED", r"unable to extract|make sure you are using the latest version|please update|yt-dlp is out of date|is no longer supported|unsupported url|extractor error|nsig extraction failed|player.*(unavailable|extract)",
     "Устаревшая версия yt-dlp или изменение сайта",
     "Сайт изменил структуру, и текущая версия yt-dlp не умеет её разбирать.",
     "Обновите yt-dlp (канал Stable, при необходимости Nightly) в разделе «Компоненты и обновления».",
     True, ["update_ytdlp"]),
]

_COMPILED = [(c, re.compile(p, re.IGNORECASE), t, ca, so, r, a) for c, p, t, ca, so, r, a in _RULES]


def classify(text: str, returncode: int | None = None) -> ErrorInfo:
    """Определяет тип ошибки по тексту журнала yt-dlp. Подробности очищаются от секретов."""
    technical = clean(text or "")[-4000:]
    haystack = text or ""
    for code, rx, title, cause, solution, retryable, actions in _COMPILED:
        if rx.search(haystack):
            return ErrorInfo(code, title, cause, solution, retryable, list(actions), technical)
    return ErrorInfo(
        "UNKNOWN", "Неизвестная ошибка загрузки",
        "yt-dlp завершился с ошибкой, которую не удалось классифицировать"
        + (f" (код {returncode})." if returncode is not None else "."),
        "Откройте технические подробности, обновите yt-dlp и повторите. Если ошибка повторяется — "
        "скопируйте журнал (без cookies и токенов) и экспортируйте отчёт из раздела «Диагностика».",
        True, ["update_ytdlp"], technical,
    )


def classify_exception(exc: BaseException) -> ErrorInfo:
    return classify(f"{type(exc).__name__}: {exc}")
