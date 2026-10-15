import argparse
import logging
import sys, os
import urllib.error
import urllib.parse
import urllib.request
from urllib.parse import urljoin, urlparse, unquote, quote
import aiohttp.client_exceptions
from bs4 import BeautifulSoup
from datetime import datetime
from contextlib import suppress
import random
import re
import gzip
import shutil


import asyncio
import aiofiles
import aiohttp
from aiohttp import ClientSession, TCPConnector
import aiosqlite
import aiofiles.os as aio_os
from emby_refresh import refresh_emby_after_sync
from sync_logging import (
    SyncStats, crawler_timeout, log_startup, log_proxy_route,
    positive_env, report_progress, log_progress, safe_url, tracked_request,
)


logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
logger = logging.getLogger("emd")
logging.getLogger("chardet.charsetprober").disabled = True


s_paths_all = [
    quote("115/"),  # 1024
    quote("PikPak/"),  # 512
    quote("动漫/"),  # 256
    quote("每日更新/"),  # 128
    quote("电影/"),  # 64
    quote("电视剧/"),  # 32
    quote("纪录片/"),  # 16
    quote("纪录片（已刮削）/"),  # 8
    quote("综艺/"),  # 4
    quote("音乐/"),  # 2
    quote("📺画质演示测试（4K，8K，HDR，Dolby）/"),  # 1
]

t_paths = [
    quote("115/"),
    quote("每日更新/"),
    quote("纪录片（已刮削）/"),
    quote("音乐/"),
    quote("综艺/"),
]

s_paths = [
    quote("115/"),
    quote("每日更新/"),
    quote("纪录片（已刮削）/"),
    quote("音乐/"),
    quote("综艺/"),
]

s_pool = []


def fetch_pool_from_url(url="https://img.xiaoya.pro/crawlerlist.txt"):
    """Fetch server pool list from remote URL"""
    try:
        log_proxy_route(url)
        logger.info("Fetching server pool from %s", url)
        response = urllib.request.urlopen(url, timeout=10)
        if response.getcode() == 200:
            content = response.read().decode("utf-8")
            pool = []
            for line in content.strip().split('\n'):
                line = line.strip()
                if line and not line.startswith('#'):
                    # Add https:// prefix if not present
                    if not line.startswith('http'):
                        line = 'https://' + line
                    # Ensure URL ends with /
                    if not line.endswith('/'):
                        line += '/'

                    # Handle IDN (Internationalized Domain Names) encoding
                    try:
                        parsed = urlparse(line)
                        # Encode the hostname using IDN
                        encoded_hostname = parsed.hostname.encode('idna').decode('ascii')
                        # Reconstruct URL with encoded hostname
                        line = f"{parsed.scheme}://{encoded_hostname}/"
                    except (UnicodeError, AttributeError):
                        # If encoding fails, skip this URL
                        logger.warning("Skipping invalid domain: %s", line)
                        continue

                    pool.append(line)
            if pool:
                logger.info("Successfully fetched %d servers from pool", len(pool))
                return pool
            else:
                logger.warning("No valid URLs found in remote list")
        else:
            logger.warning("Failed to fetch pool, status code: %s", response.getcode())
    except Exception as e:
        logger.error("Error fetching pool from %s: %s", url, e)

    # Fallback to default pool
    logger.info("Using fallback server pool")
    return [
        "https://emby.xiaoya.pro/",
        "https://icyou.eu.org/",
        "https://emby.8.net.co/",
        "https://emby.raydoom.tk/",
        "https://emby.kaiserver.uk/",
        "https://embyxiaoya.laogl.top/",
        "https://emby-data.poxi1221.eu.org/",
        "https://emby-data.ermaokj.cn/",
        "https://emby-data.bdbd.fun/",
        "https://emby-data.wwwh.eu.org/",
        "https://emby-data.f1rst.top/",
        "https://emby-data.ymschh.top/",
        "https://emby-data.wx1.us.kg/",
        "https://emby-data.r2s.site/",
        "https://emby-data.neversay.eu.org/",
        "https://emby-data.800686.xyz/",
    ]

s_folder = [".sync"]

s_ext = [".ass", ".srt", ".ssa"]

# CF blocks urllib...

CUSTOM_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/96.0.4664.110 Safari/537.36"
opener = urllib.request.build_opener()
opener.addheaders = [("User-Agent", CUSTOM_USER_AGENT)]
urllib.request.install_opener(opener)


def pick_a_pool_member(url_list):
    random.shuffle(url_list)
    for member in url_list:
        try:
            logger.info("探测镜像：%s（超时15秒）", safe_url(member))
            log_proxy_route(member)
            response = urllib.request.urlopen(member, timeout=15)
            if response.getcode() == 200:
                content = response.read()
                try:
                    content_decoded = content.decode("utf-8")
                    if "每日更新" in content_decoded:
                        logger.info("Picked: %s", member)
                        return member
                    else:
                        logger.info("Content at %s does not contain '每日更新'", member)
                except UnicodeDecodeError:
                    logger.info("Non-UTF-8 content at %s", member)
        except Exception as e:
            logger.info("Error accessing %s: %s", member, e)
    return None


def current_amount(url, media, paths):
    listfile = os.path.join(media, ".scan.list.gz")
    try:
        logger.info("正在下载远端文件清单：%s", safe_url(url))
        with urllib.request.urlopen(url, timeout=30) as response, open(listfile, "wb") as output:
            shutil.copyfileobj(response, output)
        logger.info("正在统计所选目录的远端文件数量")
        with gzip.open(listfile) as response:
            pattern = r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2} \/(.*)$"
            hidden_pattern = r"^.*?\/\..*$"
            matching_lines = 0
            for line in response:
                try:
                    line = line.decode(encoding="utf-8").strip()
                    match = re.match(pattern, line)
                    if match:
                        file = match.group(1)
                        if any(file.startswith(unquote(path)) for path in paths):
                            if not re.match(
                                hidden_pattern, file
                            ) and not file.lower().endswith(".txt"):
                                matching_lines += 1
                except:
                    logger.error("Error decoding line: %s", line)
        return matching_lines
    except (urllib.error.URLError, OSError, EOFError) as e:
        logger.error("远端清单读取失败：%s | %s", safe_url(url), type(e).__name__)
        return -1


async def fetch_html(url, session, **kwargs) -> str:
    semaphore = kwargs["semaphore"]
    stats = kwargs.get("stats", SyncStats())
    async with semaphore:
        async with tracked_request(session, url, stats, "目录") as resp:
            resp.raise_for_status()
            logger.debug("目录响应：HTTP %s %s", resp.status, safe_url(url))
            try:
                text = await resp.text()
                return text
            except UnicodeDecodeError:
                logger.error("Non-UTF-8 content at %s", unquote(url))
                return None


async def parse(url, session, **kwargs) -> set:
    stats = kwargs.get("stats", SyncStats())
    files = []
    directories = []
    try:
        html = await fetch_html(url=url, session=session, **kwargs)
        if html is None:
            stats.errors += 1
            logger.debug("Failed to fetch HTML content for URL: %s", unquote(url))
            return files, directories
    except (
        aiohttp.ClientError,
        aiohttp.http_exceptions.HttpProcessingError,
        aiohttp.ClientPayloadError,
        aiohttp.ClientResponseError,
    ) as e:
        stats.errors += 1
        logger.error("目录请求失败：%s | %s | HTTP=%s", safe_url(url), type(e).__name__, getattr(e, "status", "n/a"))
        return files, directories
    except Exception as e:
        stats.errors += 1
        logger.error("目录请求失败：%s | %s", safe_url(url), type(e).__name__)
        return files, directories

    soup = BeautifulSoup(html, "html.parser")
    for link in soup.find_all("a"):
        href = link.get("href")
        if (
            href != "../"
            and not href.endswith("/")
            and not href.endswith("txt")
            and href != "scan.list"
        ):
            try:
                abslink = urljoin(url, href)
                filename = unquote(urlparse(abslink).path)
                timestamp_str = link.next_sibling.strip().split()[0:2]
                # TODO: Need to handle /cdn-cgi/l/email-protection
                timestamp = datetime.strptime(" ".join(timestamp_str), "%d-%b-%Y %H:%M")
                timestamp_unix = int(timestamp.timestamp())
                filesize = link.next_sibling.strip().split()[2]
                files.append((abslink, filename, timestamp_unix, filesize))
            except (urllib.error.URLError, ValueError):
                stats.errors += 1
                logger.exception("Error parsing URL: %s", unquote(link))
                continue
            except Exception as e:
                stats.errors += 1
                logger.exception("Unexpected error: %s", e)
                continue
        elif href != "../" and not href.lower().endswith(".txt"):
            directories.append(urljoin(url, href))
    stats.directories += 1
    stats.files += len(files)
    return files, directories


async def need_download(file, **kwargs):
    url, filename, timestamp, filesize = file
    file_path = os.path.join(kwargs["media"], filename.lstrip("/"))
    if not os.path.exists(file_path):
        logger.debug("%s doesn't exists", file_path)
        return True
    elif file_path.endswith(".nfo"):
        if not kwargs["nfo"]:
            return False
    current_filesize = os.path.getsize(file_path)
    current_timestamp = os.path.getmtime(file_path)
    logger.debug("%s has timestamp: %s and size: %s", filename, timestamp, filesize)
    if int(filesize) == int(current_filesize) and int(timestamp) <= int(
        current_timestamp
    ):
        return False
    logger.debug(
        "%s has current_timestamp: %s and current_size: %s",
        filename,
        current_timestamp,
        current_filesize,
    )
    return True


async def download(file, session, **kwargs):
    stats = kwargs.get("stats", SyncStats())
    url, filename, timestamp, filesize = file
    semaphore = kwargs["semaphore"]
    async with semaphore:
        try:
            async with tracked_request(session, url, stats, "下载") as response:
                if response.status == 200:
                    file_path = os.path.join(kwargs["media"], filename.lstrip("/"))
                    os.umask(0)
                    os.makedirs(os.path.dirname(file_path), mode=0o777, exist_ok=True)
                    async with aiofiles.open(file_path, "wb") as f:
                        logger.debug("Starting to write file: %s", filename)
                        await f.write(await response.content.read())
                        logger.debug("Finish to write file: %s", filename)
                    os.chmod(file_path, 0o777)
                    stats.downloaded += 1
                    logger.info("Downloaded: %s", filename)
                else:
                    stats.errors += 1
                    logger.error(
                        "Failed to download: %s [Response code: %s]",
                        filename,
                        response.status,
                    )
        except Exception as e:
            stats.errors += 1
            logger.error(
                "文件下载失败：%s | %s | HTTP=%s | errno=%s",
                safe_url(url), type(e).__name__, getattr(e, "status", "n/a"), getattr(e, "errno", "n/a"),
            )


async def download_files(files, session, **kwargs):
    stats = kwargs.get("stats", SyncStats())
    download_tasks = set()
    for index, file in enumerate(files):
        if index and index % 1000 == 0:
            await asyncio.sleep(0)
        if await need_download(file, **kwargs) is True:
            task = asyncio.create_task(download(file, session, **kwargs))
            task.add_done_callback(download_tasks.discard)
            download_tasks.add(task)
            if len(download_tasks) > 100:
                await asyncio.gather(*download_tasks)
        else:
            stats.unchanged += 1
    await asyncio.gather(*download_tasks)


async def create_table(conn):
    try:
        async with conn.execute("""
            CREATE TABLE IF NOT EXISTS files (
                filename TEXT,
                timestamp INTEGER NULL,
                filesize INTEGER NULL)
        """):
            pass
    except Exception as e:
        logger.error("Unable to create DB due to %s", e)
        sys.exit(1)


async def insert_files(conn, items):
    await conn.executemany("INSERT OR REPLACE INTO files VALUES (?, ?, ?)", items)
    await conn.commit()


async def exam_file(file, media):
    stat = await aio_os.stat(file)
    return file[len(media) :], int(stat.st_mtime), stat.st_size


def process_folder(folder, media):
    all_items = []
    for root, dirs, files in os.walk(folder, topdown=False):
        dirs[:] = [d for d in dirs if d not in s_folder]
        for file in files:
            if not file.startswith(".") and not file.lower().endswith(tuple(s_ext)):
                file_path = os.path.join(root, file)
                try:
                    # Attempt to decode the filename to UTF-8
                    file_path.encode("utf-8")
                    relative_path = file_path[len(media) :]
                except UnicodeEncodeError:
                    # Log if the filename is not UTF-8
                    logging.error("Filename is not UTF-8 encoded: %s", file_path)
                    relative_path = None  # Handle or skip the invalid path as needed
                if relative_path:
                    all_items.append((relative_path, None, None))
    return all_items


def remove_empty_folders(paths, media):
    for path in paths:
        for root, dirs, files in os.walk(
            unquote(os.path.join(media, path)), topdown=False
        ):
            dirs[:] = [d for d in dirs if d not in s_folder]
            if not dirs and not files:
                try:
                    os.rmdir(root)
                    logger.info("Deleted empty folder: %s", root)
                except OSError as e:
                    logger.error("Failed to delete folder %s: %s", root, e)


async def generate_localdb(db, media, paths):
    logger.warning(
        "Generating local DB... It takes time depends on the DiskI/O performance... Do NOT quit..."
    )
    async with aiosqlite.connect(db) as conn:
        await create_table(conn)
        for path in paths:
            logger.info("Processing %s", unquote(os.path.join(media, path)))
            items = process_folder(unquote(os.path.join(media, path)), media)
            await insert_files(conn, items)
        total_items_count = await get_total_items_count(conn)
        logger.info("There are %d files on the local disk", total_items_count)


async def get_total_items_count(conn):
    async with conn.execute("SELECT COUNT(*) FROM files") as cursor:
        result = await cursor.fetchone()
        total_count = result[0] if result else 0
    return total_count


async def write_one(url, session, db_session, **kwargs) -> list:
    # This is a hack.. To be compatible with the website with the full data rather than updating ones.
    if urlparse(url).path == "/":
        directories = []
        for path in kwargs["paths"]:
            directories.append(urljoin(url, path))
        return directories
    files, directories = await parse(url=url, session=session, **kwargs)
    if not files:
        return directories
    if kwargs["media"]:
        await download_files(files=files, session=session, **kwargs)
    if db_session:
        items = []
        for file in files:
            items.append(file[1:])
        await db_session.executemany(
            "INSERT OR REPLACE INTO files VALUES (?, ?, ?)", items
        )
        await db_session.commit()
        logger.debug("Wrote results for source URL: %s", unquote(url))
    return directories


async def bulk_crawl_and_write(url, session, db_session, depth=0, **kwargs) -> None:
    tasks = set()
    directories = await write_one(
        url=url, session=session, db_session=db_session, **kwargs
    )
    for url in directories:
        if depth == 0:
            logger.info("开始扫描所选目录：%s", safe_url(url))
        task = asyncio.create_task(
            bulk_crawl_and_write(
                url=url,
                session=session,
                db_session=db_session,
                depth=depth + 1,
                **kwargs,
            )
        )
        task.add_done_callback(tasks.discard)
        tasks.add(task)
        if depth == 0:
            await asyncio.gather(*tasks)
            logger.info("所选目录扫描结束：%s", safe_url(url))
    await asyncio.gather(*tasks)


async def compare_databases(localdb, tempdb, total_amount, stats=None):
    async with aiosqlite.connect(localdb) as conn1, aiosqlite.connect(tempdb) as conn2:
        cursor1 = await conn1.cursor()
        cursor2 = await conn2.cursor()

        await cursor1.execute("SELECT filename FROM files")
        local_filenames = set(filename[0] for filename in await cursor1.fetchall())

        await cursor2.execute("SELECT filename FROM files")
        temp_filenames = set(filename[0] for filename in await cursor2.fetchall())
        gap = abs(len(temp_filenames) - total_amount)

        if gap < 10 and total_amount > 0:
            if stats is not None:
                stats.purge_status = "校验通过"
            if not gap == 0:
                logger.warning(
                    "远端清单有小幅差异：清单=%d，实际扫描=%d，绝对差值=%d（小于10）；按原有阈值继续清理",
                    total_amount,
                    len(temp_filenames),
                    abs(len(temp_filenames) - total_amount),
                )
            diff_filenames = local_filenames - temp_filenames
            return diff_filenames
        else:
            if stats is not None:
                stats.purge_status = f"已跳过（清单差值{len(temp_filenames) - total_amount:+d}）"
            logger.warning(
                "远端清单校验不一致：清单=%d，实际扫描=%d，差值=%+d；为避免误删，本轮跳过过期文件清理",
                total_amount, len(temp_filenames), len(temp_filenames) - total_amount,
            )
            return []


async def purge_removed_files(localdb, tempdb, media, total_amount, stats=None):
    for file in await compare_databases(localdb, tempdb, total_amount, stats=stats):
        try:
            os.remove(media + file)
            logger.info("已删除过期文件：%s", file)
            if stats is not None:
                stats.deleted += 1
        except Exception as e:
            if stats is not None:
                stats.errors += 1
                stats.purge_status = "部分删除失败"
            logger.error("Unable to remove %s due to %s", file, e)
    if stats is not None and stats.purge_status == "校验通过":
        stats.purge_status = "已完成"


def test_media_folder(media, paths):
    t_paths = [os.path.join(media, unquote(path)) for path in paths]
    if all(os.path.exists(os.path.abspath(path)) for path in t_paths):
        return True
    else:
        return False


def test_db_folder(location):
    if not os.path.isdir(location):
        logging.error("The path %s is not a directory.", location)
        return False
    if not os.access(location, os.W_OK):
        logging.error("The directory %s doesn't have write permission.", location)
        return False
    return True


def load_paths_from_file(path_file):
    paths = []
    try:
        with open(path_file, "r", encoding="utf-8") as file:
            for line in file:
                stripped_line = line.strip()
                if stripped_line:
                    encoded_path = quote(stripped_line)
                    if is_subpath(encoded_path, s_paths_all):
                        paths.append(encoded_path)
                    else:
                        logging.error("Path is invalid: %s", unquote(encoded_path))
                        return []
    except Exception as e:
        logging.error("Error loading paths from file: %s", str(e))
    return paths


def is_subpath(path, base_paths):
    for base_path in base_paths:
        if path.startswith(base_path):
            return True
    return False


def get_paths_from_bitmap(bitmap, paths_all):
    max_bitmap_value = (1 << len(paths_all)) - 1
    if bitmap < 0 or bitmap > max_bitmap_value:
        raise ValueError(
            f"Bitmap value {bitmap} is out of range. Must be between 0 and {max_bitmap_value}."
        )
    selected_paths = []
    binary_representation = bin(bitmap)[2:].zfill(len(paths_all))
    for i, bit in enumerate(binary_representation):
        if bit == "1":
            selected_paths.append(paths_all[i])
    return selected_paths


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--media",
        metavar="<folder>",
        type=str,
        default=None,
        required=True,
        help="Path to store downloaded media files [Default: %(default)s]",
    )
    parser.add_argument(
        "--count",
        metavar="[number]",
        type=int,
        default=100,
        help="Max concurrent HTTP Requests [Default: %(default)s]",
    )
    parser.add_argument(
        "--debug",
        action=argparse.BooleanOptionalAction,
        type=bool,
        default=False,
        help="Verbose debug [Default: %(default)s]",
    )
    parser.add_argument(
        "--db",
        action=argparse.BooleanOptionalAction,
        type=bool,
        default=False,
        help="<Python3.12+ required> Save into DB [Default: %(default)s]",
    )
    parser.add_argument(
        "--nfo",
        action=argparse.BooleanOptionalAction,
        type=bool,
        default=False,
        help="Download NFO [Default: %(default)s]",
    )
    parser.add_argument(
        "--url",
        metavar="[url]",
        type=str,
        default=None,
        help="Download path [Default: %(default)s]",
    )
    parser.add_argument(
        "--purge",
        action=argparse.BooleanOptionalAction,
        type=bool,
        default=True,
        help="Purge removed files [Default: %(default)s]",
    )
    parser.add_argument(
        "--all",
        action=argparse.BooleanOptionalAction,
        type=bool,
        default=False,
        help="Download all folders [Default: %(default)s]",
    )
    parser.add_argument(
        "--location",
        metavar="<folder>",
        type=str,
        default=None,
        required=None,
        help="Path to store database files [Default: %(default)s]",
    )
    parser.add_argument(
        "--paths",
        metavar="<file>",
        type=str,
        help="Bitmap of paths or a file containing paths to be selected (See paths.example)",
    )

    args = parser.parse_args()
    if args.count <= 0:
        parser.error("--count 必须为正整数")
    if args.debug:
        logging.getLogger("emd").setLevel(logging.DEBUG)
    try:
        timeout = crawler_timeout()
        progress_interval = positive_env("PROGRESS_INTERVAL", 15)
    except ValueError as error:
        parser.error(str(error))
    log_startup(timeout, progress_interval)

    # Fetch server pool dynamically
    global s_pool
    s_pool = fetch_pool_from_url()

    paths = []
    if args.all:
        paths = s_paths_all
        s_pool.pop(0)
        if args.purge:
            args.db = True
    else:
        if args.paths:
            paths_from_file = []
            is_bitmap = False

            try:
                paths_bitmap = int(args.paths)
                paths_from_file = get_paths_from_bitmap(paths_bitmap, s_paths_all)
                is_bitmap = True
            except ValueError:
                logging.info(
                    "Paths parameter is not a bitmap, attempting to load from file."
                )

            if not is_bitmap:
                paths_from_file = load_paths_from_file(args.paths)

            if not paths_from_file:
                logging.error(
                    "Paths file doesn't contain any valid paths or bitmap value is incorrect: %s",
                    args.paths,
                )
                sys.exit(1)

            for path in paths_from_file:
                if not is_subpath(path, s_paths):
                    s_pool.pop(0)
                    break
            paths.extend(paths_from_file)
        if not paths:
            paths = s_paths

    if args.media:
        if not os.path.exists(os.path.join(args.media, "115")):
            logging.warning(
                "115 folder doesn't exist. Creating it anyway...This workaround will be removed in the next version."
            )
            os.makedirs(os.path.join(args.media, "115"))
        if not test_media_folder(args.media, t_paths):
            logging.error(
                "The %s doesn't contain the desired folders, please correct the --media parameter",
                args.media,
            )
            sys.exit(1)
        else:
            media = args.media.rstrip("/")
    if not args.url:
        url = pick_a_pool_member(s_pool)
    else:
        url = args.url
    if not url:
        logger.info(
            "No servers are reachable, please check your Internet connection..."
        )
        sys.exit(1)
    if urlparse(url).path != "/" and (args.purge or args.db):
        logger.warning("--db or --purge only support in root path mode")
        sys.exit(1)
    stats = SyncStats()
    if urlparse(url).path == "/":
        total_amount = current_amount(url + ".scan.list.gz", media, paths)
        if total_amount < 0:
            stats.errors += 1
        logger.info("远端清单统计：所选范围共 %d 个文件 | %s", total_amount, safe_url(url))
    semaphore = asyncio.Semaphore(args.count)
    db_session = None
    if args.db or args.purge:
        assert sys.version_info >= (3, 12), "DB function requires Python 3.12+."
        if args.location:
            if test_db_folder(args.location) is True:
                db_location = args.location.rstrip("/")
            else:
                sys.exit(1)
        else:
            db_location = media
        localdb = os.path.join(db_location, ".localfiles.db")
        tempdb = os.path.join(db_location, ".tempfiles.db")
        if not os.path.exists(localdb):
            await generate_localdb(localdb, media, paths)
        elif args.db:
            os.remove(localdb)
            await generate_localdb(localdb, media, paths)
        else:
            async with aiosqlite.connect(localdb) as local_session:
                local_amount = await get_total_items_count(local_session)
                if (
                    local_amount > 0
                    and total_amount > 0
                    and abs(total_amount - local_amount) > 1000
                ):
                    logger.warning("The local DB isn't intact. regenerating...")
                    await local_session.execute("DELETE FROM files")
                    await local_session.commit()
                    await generate_localdb(localdb, media, paths)

        db_session = await aiosqlite.connect(tempdb)
        await create_table(db_session)
    logger.info("开始扫描：最大并发请求=%d；无文件需要更新时也会定时输出进度", args.count)
    async with ClientSession(
        connector=TCPConnector(ssl=False, limit=0, ttl_dns_cache=600),
        timeout=timeout,
        trust_env=True,
    ) as session:
        progress = asyncio.create_task(report_progress(stats, progress_interval))
        try:
            await bulk_crawl_and_write(
                url=url,
                session=session,
                db_session=db_session,
                semaphore=semaphore,
                media=media,
                nfo=args.nfo,
                paths=paths,
                stats=stats,
            )
        finally:
            progress.cancel()
            with suppress(asyncio.CancelledError):
                await progress
    log_progress(stats)
    logger.info("扫描完成，正在收尾")
    if db_session:
        await db_session.commit()
        await db_session.close()
    if args.purge:
        await purge_removed_files(localdb, tempdb, media, total_amount, stats=stats)
        remove_empty_folders(paths, media)
        os.remove(localdb)
        if not args.all:
            os.rename(tempdb, localdb)
        else:
            os.remove(tempdb)
    logger.info(
        "同步汇总：扫描文件=%d，下载成功=%d，无需下载=%d，操作错误=%d，过期清理=%s，删除文件=%d",
        stats.files, stats.downloaded, stats.unchanged, stats.errors, stats.purge_status, stats.deleted,
    )
    if not await refresh_emby_after_sync(stats.downloaded, stats.errors):
        logger.warning("元数据同步已结束，但 Emby 刷新未全部成功，请查看上方刷新错误")
    logger.info("本轮同步流程结束")


if __name__ == "__main__":
    assert sys.version_info >= (3, 10), "Script requires Python 3.10+."
    asyncio.run(main())
