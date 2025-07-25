"""osu: handle connections from web, api, and beyond?"""

from __future__ import annotations

import copy
import hashlib
import os
import random
import secrets
import tempfile
from collections import defaultdict
from collections.abc import Awaitable
from collections.abc import Callable
from collections.abc import Mapping
from datetime import datetime
from enum import IntEnum
from enum import unique
from functools import cache
from pathlib import Path as SystemPath
from typing import Any
from typing import Literal
from typing import cast
from urllib.parse import unquote
from urllib.parse import unquote_plus

import bcrypt
from fastapi import status
from fastapi.datastructures import FormData
from fastapi.datastructures import UploadFile
from fastapi.exceptions import HTTPException
from fastapi.param_functions import Depends
from fastapi.param_functions import File
from fastapi.param_functions import Form
from fastapi.param_functions import Header
from fastapi.param_functions import Path
from fastapi.param_functions import Query
from fastapi.requests import Request
from fastapi.responses import FileResponse
from fastapi.responses import ORJSONResponse
from fastapi.responses import RedirectResponse
from fastapi.responses import Response
from fastapi.routing import APIRouter
from starlette.datastructures import UploadFile as StarletteUploadFile

import app
import app.packets
import app.settings
import app.state
import app.state.services
import app.storage
import app.utils
from app import encryption
from app._typing import UNSET
from app.constants import regexes
from app.constants.clientflags import LastFMFlags
from app.constants.gamemodes import GameMode
from app.constants.mods import Mods
from app.constants.privileges import Privileges
from app.logging import Ansi
from app.logging import log
from app.objects import models
from app.objects.beatmap import Beatmap
from app.objects.beatmap import BeatmapSet
from app.objects.beatmap import RankedStatus
from app.objects.beatmap import ensure_osu_file_is_available
from app.objects.player import Player
from app.objects.score import Grade
from app.objects.score import Score
from app.objects.score import SubmissionStatus
from app.repositories import clans as clans_repo
from app.repositories import comments as comments_repo
from app.repositories import custom_maps as custom_maps_repo
from app.repositories import custom_scores as custom_scores_repo
from app.repositories import favourites as favourites_repo
from app.repositories import mail as mail_repo
from app.repositories import maps as maps_repo
from app.repositories import ratings as ratings_repo
from app.repositories import scores as scores_repo
from app.repositories import stats as stats_repo
from app.repositories import users as users_repo
from app.repositories.achievements import Achievement
from app.usecases import achievements as achievements_usecases
from app.usecases import performance as performance_usecases
from app.usecases import user_achievements as user_achievements_usecases
from app.usecases.performance import ScoreParams
from app.utils import escape_enum
from app.utils import pymysql_encode

BEATMAPS_PATH = SystemPath.cwd() / ".data/osu"
REPLAYS_PATH = SystemPath.cwd() / ".data/osr"
SCREENSHOTS_PATH = SystemPath.cwd() / ".data/ss"


router = APIRouter(
    tags=["osu! web API"],
    default_response_class=Response,
)


async def get_custom_map_osu_file(map_md5: str) -> str | None:
    """从user_backend获取自定义谱面的.osu文件内容"""
    try:
        # 使用app.state.services.http_client
        # TODO: 使用环境变量或配置文件来获取URL
        # print(f"Fetching osu file for map MD5: {map_md5}")
        response = await app.state.services.http_client.get(
            f"https://a.gu-osu.gmoe.cc/api/osu-files/{map_md5}",
        )
        if response.status_code == 200:
            return response.text
        return None
    except Exception as e:
        log(f"Failed to fetch .osu file for {map_md5}: {e}", Ansi.LRED)
        return None


async def calculate_custom_map_performance(
    score: Score,
    osu_file_content: str,
) -> tuple[float, float]:
    """使用官方PP计算方法计算自定义谱面PP"""
    try:
        # 创建临时.osu文件
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".osu",
            delete=False,
            encoding="utf-8",
        ) as f:
            f.write(osu_file_content)
            temp_osu_path = f.name

        try:
            mode_vn = score.mode.as_vanilla

            score_args = ScoreParams(
                mode=mode_vn,
                mods=int(score.mods),
                combo=score.max_combo,
                ngeki=score.ngeki,
                n300=score.n300,
                nkatu=score.nkatu,
                n100=score.n100,
                n50=score.n50,
                nmiss=score.nmiss,
            )

            result = performance_usecases.calculate_performances(
                osu_file_path=temp_osu_path,
                scores=[score_args],
            )

            if result:
                return result[0]["performance"]["pp"], result[0]["difficulty"]["stars"]
            else:
                return 0.0, 1.0

        finally:
            # 清理临时文件
            try:
                os.unlink(temp_osu_path)
            except:
                pass

    except Exception as e:
        log(f"Error calculating PP for custom map: {e}", Ansi.LRED)
        return 0.0, 1.0


def calculate_fallback_performance(
    score: Score,
    custom_beatmap: dict,
) -> tuple[float, float]:
    """简化的PP计算方法作为备用"""
    star_rating = custom_beatmap.get("star_rating", 1.0) or 1.0
    od = custom_beatmap.get("overall_difficulty", 5.0) or 5.0
    ar = custom_beatmap.get("approach_rate", 5.0) or 5.0
    cs = custom_beatmap.get("circle_size", 4.0) or 4.0
    hp = custom_beatmap.get("hp_drain_rate", 5.0) or 5.0
    max_combo = custom_beatmap.get("max_combo", 1000) or 1000

    # 基于acc和星级的简化PP计算
    acc_factor = score.acc / 100.0

    # 准确度奖励 (非线性)
    acc_bonus = 1.0
    if acc_factor > 0.95:
        acc_bonus = 1.0 + (acc_factor - 0.95) * 4  # 95%以上有额外奖励
    elif acc_factor > 0.9:
        acc_bonus = 0.8 + (acc_factor - 0.9) * 4  # 90-95%正常
    else:
        acc_bonus = acc_factor * 0.8  # 90%以下惩罚

    # Combo奖励
    combo_factor = min(score.max_combo / max_combo, 1.0) ** 0.8

    # Miss惩罚
    miss_penalty = 1.0
    if score.nmiss > 0:
        miss_penalty = 0.97**score.nmiss

    # Mod奖励
    mod_multiplier = 1.0
    if score.mods:
        # 简化的mod计算
        if score.mods & Mods.HIDDEN:
            mod_multiplier *= 1.06
        if score.mods & Mods.HARDROCK:
            mod_multiplier *= 1.10
        if score.mods & Mods.DOUBLETIME:
            mod_multiplier *= 1.12
        if score.mods & Mods.FLASHLIGHT:
            mod_multiplier *= 1.12
        if score.mods & Mods.EASY:
            mod_multiplier *= 0.50
        if score.mods & Mods.HALFTIME:
            mod_multiplier *= 0.30
        if score.mods & Mods.NOFAIL:
            mod_multiplier *= 0.90

    # 最终PP计算
    base_pp = (star_rating**2.4) * 15  # 基础PP
    pp = base_pp * acc_bonus * combo_factor * miss_penalty * mod_multiplier

    # 限制最大PP
    pp = min(pp, 9999.999)

    return pp, star_rating


async def calculate_custom_map_status(score: Score, custom_beatmap: dict) -> Any | None:
    """Calculate the submission status of a custom map score - 与官方谱面逻辑一致

    Returns:
        CustomScore | None: 之前的最佳成绩数据，如果没有则返回None
    """
    assert score.player is not None

    # 查找用户在此自定义谱面上的现有最佳成绩
    existing_best_score = await custom_scores_repo.fetch_one(
        map_md5=custom_beatmap["md5"],
        user_id=score.player.id,
        mode=score.mode,
        status=2,  # best score
    )

    if existing_best_score:
        # 我们已经在这张谱面上有成绩了
        # 使用与官方谱面相同的比较逻辑
        # 对于自定义谱面，我们主要比较PP（如果有的话），否则比较分数
        if score.pp > 0 and existing_best_score["pp"] > 0:
            # 两个都有PP，比较PP
            if score.pp > existing_best_score["pp"]:
                score.status = SubmissionStatus.BEST
                return existing_best_score
            else:
                score.status = SubmissionStatus.SUBMITTED
                return None
        else:
            # 比较分数
            if score.score > existing_best_score["score"]:
                score.status = SubmissionStatus.BEST
                return existing_best_score
            else:
                score.status = SubmissionStatus.SUBMITTED
                return None
    else:
        # 这是我们在此谱面上的第一个成绩
        score.status = SubmissionStatus.BEST
        return None


async def calculate_custom_map_placement(score: Score, custom_beatmap: dict) -> int:
    """Calculate the placement/rank of a custom map score - 与官方谱面逻辑一致"""
    # 确定使用哪个计分指标
    if score.mode >= GameMode.RELAX_OSU:
        scoring_metric = "pp"
        score_value = score.pp
    else:
        scoring_metric = "score"
        score_value = score.score

    # 计算有多少更好的成绩
    num_better_scores: int | None = await app.state.services.database.fetch_val(
        "SELECT COUNT(*) AS c FROM custom_scores cs "
        "INNER JOIN users u ON u.id = cs.user_id "
        "WHERE cs.map_md5 = :map_md5 AND cs.mode = :mode "
        "AND cs.status = 2 AND u.priv & 1 "  # 只计算最佳成绩且用户有权限
        f"AND cs.{scoring_metric} > :score",
        {
            "map_md5": custom_beatmap["md5"],
            "mode": score.mode,
            "score": score_value,
        },
        column=0,  # COUNT(*)
    )

    if num_better_scores is None:
        num_better_scores = 0

    return num_better_scores + 1


@cache
def authenticate_player_session(
    param_function: Callable[..., Any],
    username_alias: str = "u",
    pw_md5_alias: str = "p",
    err: Any | None = None,
) -> Callable[[str, str], Awaitable[Player]]:
    async def wrapper(
        username: str = param_function(..., alias=username_alias),
        pw_md5: str = param_function(..., alias=pw_md5_alias),
    ) -> Player:
        player = await app.state.sessions.players.from_login(
            name=unquote(username),
            pw_md5=pw_md5,
        )
        if player:
            return player

        # player login incorrect
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=err,
        )

    return wrapper


""" /web/ handlers """

# Unhandled endpoints:
# POST /web/osu-error.php
# POST /web/osu-session.php
# POST /web/osu-osz2-bmsubmit-post.php
# POST /web/osu-osz2-bmsubmit-upload.php
# GET /web/osu-osz2-bmsubmit-getid.php
# GET /web/osu-get-beatmap-topic.php


@router.post("/web/osu-screenshot.php")
async def osuScreenshot(
    player: Player = Depends(authenticate_player_session(Form, "u", "p")),
    endpoint_version: int = Form(..., alias="v"),
    screenshot_file: UploadFile = File(..., alias="ss"),
) -> Response:
    with memoryview(await screenshot_file.read()) as screenshot_view:
        # png sizes: 1080p: ~300-800kB | 4k: ~1-2mB
        if len(screenshot_view) > (4 * 1024 * 1024):
            return Response(
                content=b"Screenshot file too large.",
                status_code=status.HTTP_400_BAD_REQUEST,
            )

        if endpoint_version != 1:
            await app.state.services.log_strange_occurrence(
                f"Incorrect endpoint version (/web/osu-screenshot.php v{endpoint_version})",
            )

        if app.utils.has_jpeg_headers_and_trailers(screenshot_view):
            extension = "jpeg"
        elif app.utils.has_png_headers_and_trailers(screenshot_view):
            extension = "png"
        else:
            return Response(
                content=b"Invalid file type",
                status_code=status.HTTP_400_BAD_REQUEST,
            )

        while True:
            filename = f"{secrets.token_urlsafe(6)}.{extension}"
            ss_file = SCREENSHOTS_PATH / filename
            if not ss_file.exists():
                break

        with ss_file.open("wb") as f:
            f.write(screenshot_view)

    log(f"{player} uploaded {filename}.")
    return Response(filename.encode())


@router.get("/web/osu-getfriends.php")
async def osuGetFriends(
    player: Player = Depends(authenticate_player_session(Query, "u", "h")),
) -> Response:
    return Response("\n".join(map(str, player.friends)).encode())


def bancho_to_osuapi_status(bancho_status: int) -> int:
    return {
        0: 0,
        2: 1,
        3: 2,
        4: 3,
        5: 4,
    }[bancho_status]


@router.post("/web/osu-getbeatmapinfo.php")
async def osuGetBeatmapInfo(
    form_data: models.OsuBeatmapRequestForm,
    player: Player = Depends(authenticate_player_session(Query, "u", "h")),
) -> Response:
    num_requests = len(form_data.Filenames) + len(form_data.Ids)
    log(f"{player} requested info for {num_requests} maps.", Ansi.LCYAN)

    response_lines: list[str] = []

    for idx, map_filename in enumerate(form_data.Filenames):
        # try getting the map from sql (official maps first)
        beatmap = await maps_repo.fetch_one(filename=map_filename)
        is_custom = False

        if not beatmap:
            # try getting from custom maps
            beatmap = await custom_maps_repo.fetch_one(filename=map_filename)
            is_custom = True

        if not beatmap:
            continue

        # try to get the user's grades on the map
        # NOTE: osu! only allows us to send back one per gamemode,
        #       so we've decided to send back *vanilla* grades.
        #       (in theory we could make this user-customizable)
        grades = ["N", "N", "N", "N"]

        # For custom maps, we need to check scores differently
        # Custom maps use their own scoring system
        if is_custom:
            # For now, we'll just use default grades for custom maps
            # TODO: Implement custom map scoring integration
            pass
        else:
            for score in await scores_repo.fetch_many(
                map_md5=beatmap["md5"],
                user_id=player.id,
                mode=player.status.mode.as_vanilla,
                status=SubmissionStatus.BEST,
            ):
                grades[score["mode"]] = score["grade"]

        # Format the response for both official and custom maps
        if is_custom:
            # Custom maps have different field names and structure
            # Custom map IDs start from 1000000 to avoid conflicts
            # Convert custom map status (str) to bancho status (int)
            custom_map = cast(custom_maps_repo.CustomMap, beatmap)
            custom_status = custom_map["status"]
            if custom_status == "pending":
                status_int = 0
            elif custom_status == "approved":
                status_int = 2
            elif custom_status == "rejected":
                status_int = -1
            elif custom_status == "loved":
                status_int = 4
            else:
                status_int = 0  # default to pending

            response_lines.append(
                "{i}|{id}|{set_id}|{md5}|{status}|{grades}".format(
                    i=idx,
                    id=custom_map["id"],  # Custom map ID (already >= 1000000)
                    set_id=custom_map["mapset_id"],  # Custom mapset ID
                    md5=custom_map["md5"],
                    status=bancho_to_osuapi_status(status_int),
                    grades="|".join(grades),
                ),
            )
        else:
            official_map = cast(maps_repo.Map, beatmap)
            response_lines.append(
                "{i}|{id}|{set_id}|{md5}|{status}|{grades}".format(
                    i=idx,
                    id=official_map["id"],
                    set_id=official_map["set_id"],
                    md5=official_map["md5"],
                    status=bancho_to_osuapi_status(official_map["status"]),
                    grades="|".join(grades),
                ),
            )

    if form_data.Ids:  # still have yet to see this used
        await app.state.services.log_strange_occurrence(
            f"{player} requested map(s) info by id ({form_data.Ids})",
        )

    return Response("\n".join(response_lines).encode())


@router.get("/web/osu-getfavourites.php")
async def osuGetFavourites(
    player: Player = Depends(authenticate_player_session(Query, "u", "h")),
) -> Response:
    favourites = await favourites_repo.fetch_all(userid=player.id)

    return Response(
        "\n".join([str(favourite["setid"]) for favourite in favourites]).encode(),
    )


@router.get("/web/osu-addfavourite.php")
async def osuAddFavourite(
    player: Player = Depends(authenticate_player_session(Query, "u", "h")),
    map_set_id: int = Query(..., alias="a"),
) -> Response:
    # check if they already have this favourited.
    if await favourites_repo.fetch_one(player.id, map_set_id):
        return Response(b"You've already favourited this beatmap!")

    # add favourite
    await favourites_repo.create(
        userid=player.id,
        setid=map_set_id,
    )

    return Response(b"Added favourite!")


@router.get("/web/lastfm.php")
async def lastFM(
    action: Literal["scrobble", "np"],
    beatmap_id_or_hidden_flag: str = Query(
        ...,
        description=(
            "This flag is normally a beatmap ID, but is also "
            "used as a hidden anticheat flag within osu!"
        ),
        alias="b",
    ),
    player: Player = Depends(authenticate_player_session(Query, "us", "ha")),
) -> Response:
    if beatmap_id_or_hidden_flag[0] != "a":
        # not anticheat related, tell the
        # client not to send any more for now.
        return Response(b"-3")

    flags = LastFMFlags(int(beatmap_id_or_hidden_flag[1:]))

    if flags & (LastFMFlags.HQ_ASSEMBLY | LastFMFlags.HQ_FILE):
        # Player is currently running hq!osu; could possibly
        # be a separate client, buuuut prooobably not lol.

        await player.restrict(
            admin=app.state.sessions.bot,
            reason=f"hq!osu running ({flags})",
        )

        # refresh their client state
        if player.is_online:
            player.logout()

        return Response(b"-3")

    if flags & LastFMFlags.REGISTRY_EDITS:
        # Player has registry edits left from
        # hq!osu's multiaccounting tool. This
        # does not necessarily mean they are
        # using it now, but they have in the past.

        if random.randrange(32) == 0:
            # Random chance (1/32) for a ban.
            await player.restrict(
                admin=app.state.sessions.bot,
                reason="hq!osu relife 1/32",
            )

            # refresh their client state
            if player.is_online:
                player.logout()

            return Response(b"-3")

        player.enqueue(
            app.packets.notification(
                "\n".join(
                    [
                        "Hey!",
                        "It appears you have hq!osu's multiaccounting tool (relife) enabled.",
                        "This tool leaves a change in your registry that the osu! client can detect.",
                        "Please re-install relife and disable the program to avoid any restrictions.",
                    ],
                ),
            ),
        )

        player.logout()

        return Response(b"-3")

    """ These checks only worked for ~5 hours from release. rumoi's quick!
    if flags & (
        LastFMFlags.SDL2_LIBRARY
        | LastFMFlags.OPENSSL_LIBRARY
        | LastFMFlags.AQN_MENU_SAMPLE
    ):
        # AQN has been detected in the client, either
        # through the 'libeay32.dll' library being found
        # onboard, or from the menu sound being played in
        # the AQN menu while being in an inappropriate menu
        # for the context of the sound effect.
        pass
    """

    return Response(b"")


DIRECT_SET_INFO_FMTSTR = (
    "{SetID}.osz|{Artist}|{Title}|{Creator}|"
    "{RankedStatus}|10.0|{LastUpdate}|{SetID}|"
    "0|{HasVideo}|0|0|0|{diffs}"  # 0s are threadid, has_story,
    # filesize, filesize_novid.
)

DIRECT_MAP_INFO_FMTSTR = (
    "[{DifficultyRating:.2f}⭐] {DiffName} "
    "{{cs: {CS} / od: {OD} / ar: {AR} / hp: {HP}}}@{Mode}"
)


@router.get("/web/osu-search.php")
async def osuSearchHandler(
    player: Player = Depends(authenticate_player_session(Query, "u", "h")),
    ranked_status: int = Query(..., alias="r", ge=0, le=8),
    query: str = Query(..., alias="q"),
    mode: int = Query(..., alias="m", ge=-1, le=3),  # -1 for all
    page_num: int = Query(..., alias="p"),
) -> Response:
    params: dict[str, Any] = {"amount": 100, "offset": page_num * 100}

    # eventually we could try supporting these,
    # but it mostly depends on the mirror.
    if query not in ("Newest", "Top+Rated", "Most+Played"):
        params["query"] = query

    if mode != -1:  # -1 for all
        params["mode"] = mode

    if ranked_status != 4:  # 4 for all
        # convert to osu!api status
        params["status"] = RankedStatus.from_osudirect(ranked_status).osu_api

    response = await app.state.services.http_client.get(
        app.settings.MIRROR_SEARCH_ENDPOINT,
        params=params,
    )
    if response.status_code != status.HTTP_200_OK:
        return Response(b"-1\nFailed to retrieve data from the beatmap mirror.")

    result = response.json()

    lresult = len(result)  # send over 100 if we receive
    # 100 matches, so the client
    # knows there are more to get
    ret = [f"{'101' if lresult == 100 else lresult}"]
    for bmapset in result:
        if bmapset["ChildrenBeatmaps"] is None:
            continue

        # some mirrors use a true/false instead of 0 or 1
        bmapset["HasVideo"] = int(bmapset["HasVideo"])

        diff_sorted_maps = sorted(
            bmapset["ChildrenBeatmaps"],
            key=lambda m: m["DifficultyRating"],
        )

        def handle_invalid_characters(s: str) -> str:
            # XXX: this is a bug that exists on official servers (lmao)
            # | is used to delimit the set data, so the difficulty name
            # cannot contain this or it will be ignored. we fix it here
            # by using a different character.
            return s.replace("|", "I")

        diffs_str = ",".join(
            [
                DIRECT_MAP_INFO_FMTSTR.format(
                    DifficultyRating=row["DifficultyRating"],
                    DiffName=handle_invalid_characters(row["DiffName"]),
                    CS=row["CS"],
                    OD=row["OD"],
                    AR=row["AR"],
                    HP=row["HP"],
                    Mode=row["Mode"],
                )
                for row in diff_sorted_maps
            ],
        )

        ret.append(
            DIRECT_SET_INFO_FMTSTR.format(
                Artist=handle_invalid_characters(bmapset["Artist"]),
                Title=handle_invalid_characters(bmapset["Title"]),
                Creator=bmapset["Creator"],
                RankedStatus=bmapset["RankedStatus"],
                LastUpdate=bmapset["LastUpdate"],
                SetID=bmapset["SetID"],
                HasVideo=bmapset["HasVideo"],
                diffs=diffs_str,
            ),
        )

    return Response("\n".join(ret).encode())


# TODO: video support (needs db change)
@router.get("/web/osu-search-set.php")
async def osuSearchSetHandler(
    player: Player = Depends(authenticate_player_session(Query, "u", "h")),
    map_set_id: int | None = Query(None, alias="s"),
    map_id: int | None = Query(None, alias="b"),
    checksum: str | None = Query(None, alias="c"),
) -> Response:
    # Since we only need set-specific data, we can basically
    # just do same query with either bid or bsid.

    v: int | str
    if map_set_id is not None:
        # this is just a normal request
        k, v = ("set_id", map_set_id)
    elif map_id is not None:
        k, v = ("id", map_id)
    elif checksum is not None:
        k, v = ("md5", checksum)
    else:
        return Response(b"")  # invalid args

    # Get all set data.
    bmapset = await app.state.services.database.fetch_one(
        "SELECT DISTINCT set_id, artist, "
        "title, status, creator, last_update "
        f"FROM maps WHERE {k} = :v",
        {"v": v},
    )
    if bmapset is None:
        # TODO: get from osu!
        return Response(b"")

    rating = 10.0  # TODO: real data

    return Response(
        (
            "{set_id}.osz|{artist}|{title}|{creator}|"
            "{status}|{rating:.1f}|{last_update}|{set_id}|"
            "0|0|0|0|0"
        )
        .format(**bmapset, rating=rating)
        .encode(),
    )
    # 0s are threadid, has_vid, has_story, filesize, filesize_novid


def chart_entry(name: str, before: float | None, after: float | None) -> str:
    return f"{name}Before:{before or ''}|{name}After:{after or ''}"


def format_achievement_string(file: str, name: str, description: str) -> str:
    return f"{file}+{name}+{description}"


async def handle_custom_map_score_submission(
    score: Score,
    custom_beatmap: dict,
    score_time: int,
    fail_time: int,
    replay_file,
    unique_ids: str,
    osu_version: str,
    client_hash_decoded: str,
    storyboard_md5: str | None,
    bmap_md5: str,
) -> Response:
    """处理自定义谱面的成绩提交"""
    # TODO: 成绩提交有问题！！！！！
    from app.objects.score import Grade
    from app.objects.score import SubmissionStatus

    # 类型检查
    if score.player is None:
        return Response(b"error: no")

    ## perform checksum validation
    # 成绩提交
    unique_id1, unique_id2 = unique_ids.split("|", maxsplit=1)
    unique_id1_md5 = hashlib.md5(unique_id1.encode()).hexdigest()
    unique_id2_md5 = hashlib.md5(unique_id2.encode()).hexdigest()

    # print(custom_beatmap)
    try:
        assert score.player.client_details is not None
        # print(client_hash_decoded, score.player.client_details.client_hash)
        if osu_version != f"{score.player.client_details.osu_version.date:%Y%m%d}":
            raise ValueError("osu! version mismatch")

        if client_hash_decoded != score.player.client_details.client_hash:
            raise ValueError("client hash mismatch")
        # assert unique ids (c1) are correct and match login params
        if unique_id1_md5 != score.player.client_details.uninstall_md5:
            raise ValueError(
                f"unique_id1 mismatch ({unique_id1_md5} != {score.player.client_details.uninstall_md5})",
            )

        if unique_id2_md5 != score.player.client_details.disk_signature_md5:
            raise ValueError(
                f"unique_id2 mismatch ({unique_id2_md5} != {score.player.client_details.disk_signature_md5})",
            )

        # assert online checksums match
        server_score_checksum = score.compute_online_checksum(
            osu_version=osu_version,
            osu_client_hash=client_hash_decoded,
            storyboard_checksum=storyboard_md5 or "",
        )
        if score.client_checksum != server_score_checksum:
            raise ValueError(
                f"online score checksum mismatch ({server_score_checksum} != {score.client_checksum})",
            )

        # assert beatmap hashes match (for custom maps, use the custom map's MD5)
        if bmap_md5 != custom_beatmap["md5"]:
            raise ValueError(
                f"beatmap hash mismatch ({bmap_md5} != {custom_beatmap['md5']})",
            )

    except (AssertionError, ValueError) as exc:
        log(f"Invalid score submission: {exc}", Ansi.LYELLOW)
        return Response(b"error: no")
    # print(custom_beatmap)
    # 计算准确率
    score.acc = score.calculate_accuracy()

    # 设置时间
    score.time_elapsed = score_time if score.passed else fail_time

    # 设置星级 - 使用与官方谱面类似的逻辑
    score.sr = custom_beatmap.get("star_rating", 1.0) or 1.0

    # PP计算和状态处理 - 与官方谱面保持一致
    # 首先计算PP和星级（无论是否通过都要计算）
    try:
        # 从user_backend获取.osu文件内容
        osu_file_content = await get_custom_map_osu_file(custom_beatmap["md5"])
        if osu_file_content:
            # 使用官方PP计算
            score.pp, score.sr = await calculate_custom_map_performance(
                score,
                osu_file_content,
            )
        else:
            # 如果无法获取.osu文件，使用简化计算
            score.pp, score.sr = calculate_fallback_performance(score, custom_beatmap)
    except Exception as e:
        # 计算失败时使用简化方法
        log(f"Failed to calculate PP for custom map: {e}", Ansi.LYELLOW)
        score.pp, score.sr = calculate_fallback_performance(score, custom_beatmap)
    # print(custom_beatmap)
    # 打印score全部参数
    # print(score)

    # 状态计算 - 与官方谱面保持一致的逻辑
    if score.passed:
        existing_score = await calculate_custom_map_status(score, custom_beatmap)

        # 计算排名 (如果是最佳成绩且谱面不是待定状态)
        # 对于自定义谱面，我们假设它们都可以计算排名
        if score.status == SubmissionStatus.BEST:
            score.rank = await calculate_custom_map_placement(score, custom_beatmap)
    else:
        # 失败的成绩状态设置为FAILED，但不修改PP值
        score.status = SubmissionStatus.FAILED
        existing_score = None

    # 保存成绩到数据库 - 与官方谱面逻辑一致
    # 如果这是最佳成绩，先更新之前的最佳成绩状态为 SUBMITTED
    if score.status == SubmissionStatus.BEST:
        # 这个成绩是新的最佳成绩，将之前的最佳成绩状态更新为 SUBMITTED
        await app.state.services.database.execute(
            "UPDATE custom_scores SET status = 1 "
            "WHERE status = 2 AND map_md5 = :map_md5 "
            "AND user_id = :user_id AND mode = :mode",
            {
                "map_md5": custom_beatmap["md5"],
                "user_id": score.player.id,
                "mode": score.mode,
            },
        )

    # 插入新成绩
    score.id = await custom_scores_repo.create(
        map_id=custom_beatmap["id"],
        map_md5=custom_beatmap["md5"],
        user_id=score.player.id,
        score=score.score,
        pp=score.pp,
        acc=score.acc,
        max_combo=score.max_combo,
        mods=int(score.mods),
        n300=score.n300,
        n100=score.n100,
        n50=score.n50,
        nmiss=score.nmiss,
        ngeki=score.ngeki,
        nkatu=score.nkatu,
        grade=score.grade.name,
        status=int(score.status),
        mode=int(score.mode),
        play_time=score.server_time,
        time_elapsed=score.time_elapsed,
        client_flags=int(score.client_flags),
        perfect=score.perfect,
        online_checksum=server_score_checksum,
    )

    # 保存回放文件
    if score.passed and score.id:
        replay_data = await replay_file.read()
        # print(replay_data)
        MIN_REPLAY_SIZE = 24

        if len(replay_data) >= MIN_REPLAY_SIZE:
            key = f"{app.settings.R2_REPLAY_FOLDER}/{score.id}2.osr"
            metadata = {
                "score-id": str(score.id),
                "score-type": "custom",
                "user-id": str(score.player.id),
                "map-md5": custom_beatmap["md5"],
                "mode": str(score.mode.value),
            }
            await app.storage.upload(
                app.settings.R2_BUCKET,
                key,
                replay_data,
                metadata=metadata,
            )

    # 更新用户统计 - 完全与官方谱面保持一致
    if score.player.stats is None:
        return Response(b"error: no")

    stats = score.player.stats[score.mode]
    prev_stats = copy.copy(stats)

    # 基本统计更新 - 对所有提交的成绩都进行更新
    stats.playtime += score.time_elapsed // 1000
    stats.plays += 1
    stats.tscore += score.score
    stats.total_hits += score.n300 + score.n100 + score.n50

    if score.mode.as_vanilla in (1, 3):
        # taiko uses geki & katu for hitting big notes with 2 keys
        # mania uses geki & katu for rainbow 300 & 200
        stats.total_hits += score.ngeki + score.nkatu

    stats_updates: dict[str, Any] = {
        "plays": stats.plays,
        "playtime": stats.playtime,
        "tscore": stats.tscore,
        "total_hits": stats.total_hits,
    }

    # 对于通过的成绩且有排行榜的自定义谱面进行额外统计更新
    # 自定义谱面假设都有排行榜
    if score.passed:
        # player passed & custom map has leaderboard

        if score.max_combo > stats.max_combo:
            stats.max_combo = score.max_combo
            stats_updates["max_combo"] = stats.max_combo

        # 自定义谱面假设都能给PP（类似approved谱面），且是最佳成绩时更新排名相关统计
        if score.status == SubmissionStatus.BEST:
            # 这是我们在此谱面上的新最佳成绩
            # 更新ranked score, grades, pp, acc 和 global rank

            additional_rscore = score.score
            if existing_score:
                # 我们之前在这张谱面上有成绩，从 ranked score 中减去它的分数
                additional_rscore -= existing_score["score"]

                # 处理成绩等级变化
                if score.grade != Grade[existing_score["grade"]]:
                    if score.grade >= Grade.A:
                        stats.grades[score.grade] += 1
                        grade_col = format(score.grade, "stats_column")
                        stats_updates[grade_col] = stats.grades[score.grade]

                    if Grade[existing_score["grade"]] >= Grade.A:
                        stats.grades[Grade[existing_score["grade"]]] -= 1
                        grade_col = format(
                            Grade[existing_score["grade"]],
                            "stats_column",
                        )
                        stats_updates[grade_col] = stats.grades[
                            Grade[existing_score["grade"]]
                        ]
            else:
                # 这是我们在此谱面上的第一个提交成绩
                if score.grade >= Grade.A:
                    stats.grades[score.grade] += 1
                    grade_col = format(score.grade, "stats_column")
                    stats_updates[grade_col] = stats.grades[score.grade]

            stats.rscore += additional_rscore
            stats_updates["rscore"] = stats.rscore

            # 获取按PP排序的成绩来计算总准确度/PP - 包含官方谱面和自定义谱面
            # 首先获取官方谱面的最佳成绩
            official_best_scores = await app.state.services.database.fetch_all(
                "SELECT s.pp, s.acc FROM scores s "
                "INNER JOIN maps m ON s.map_md5 = m.md5 "
                "WHERE s.userid = :user_id AND s.mode = :mode "
                "AND s.status = 2 AND m.status IN (2, 3) "  # ranked, approved
                "ORDER BY s.pp DESC",
                {"user_id": score.player.id, "mode": score.mode},
            )

            # 然后获取自定义谱面的最佳成绩
            custom_best_scores = await app.state.services.database.fetch_all(
                "SELECT cs.pp, cs.acc FROM custom_scores cs "
                "INNER JOIN custom_maps cm ON cs.map_md5 = cm.md5 "
                "WHERE cs.user_id = :user_id AND cs.mode = :mode "
                "AND cs.status = 2 AND cm.status IN ('approved', 'loved') "  # approved, loved
                "ORDER BY cs.pp DESC",
                {"user_id": score.player.id, "mode": score.mode},
            )

            # 合并并按PP排序所有成绩
            all_best_scores = list(official_best_scores) + list(custom_best_scores)
            all_best_scores.sort(key=lambda x: x["pp"], reverse=True)

            if all_best_scores:
                # 计算新的总加权准确度
                weighted_acc = sum(
                    row["acc"] * 0.95**i for i, row in enumerate(all_best_scores)
                )
                bonus_acc = 100.0 / (20 * (1 - 0.95 ** len(all_best_scores)))
                stats.acc = (weighted_acc * bonus_acc) / 100
                stats_updates["acc"] = stats.acc

                # 计算新的总加权PP
                weighted_pp = sum(
                    row["pp"] * 0.95**i for i, row in enumerate(all_best_scores)
                )
                bonus_pp = 416.6667 * (1 - 0.9994 ** len(all_best_scores))
                stats.pp = round(weighted_pp + bonus_pp)
                stats_updates["pp"] = stats.pp

            # 更新全球和国家排名
            stats.rank = await score.player.update_rank(score.mode)

    # 更新用户统计到数据库
    await stats_repo.partial_update(
        score.player.id,
        score.mode.value,
        plays=stats_updates.get("plays", UNSET),
        playtime=stats_updates.get("playtime", UNSET),
        tscore=stats_updates.get("tscore", UNSET),
        total_hits=stats_updates.get("total_hits", UNSET),
        max_combo=stats_updates.get("max_combo", UNSET),
        xh_count=stats_updates.get("xh_count", UNSET),
        x_count=stats_updates.get("x_count", UNSET),
        sh_count=stats_updates.get("sh_count", UNSET),
        s_count=stats_updates.get("s_count", UNSET),
        a_count=stats_updates.get("a_count", UNSET),
        rscore=stats_updates.get("rscore", UNSET),
        acc=stats_updates.get("acc", UNSET),
        pp=stats_updates.get("pp", UNSET),
    )

    if not score.player.restricted:
        # enqueue new stats info to all other users
        app.state.sessions.players.enqueue(app.packets.user_stats(score.player))

    # 更新谱面统计
    new_plays = 0
    new_passes = 0
    if not score.player.restricted:
        # 更新自定义谱面的游玩次数
        new_plays = custom_beatmap["plays"] + 1
        new_passes = custom_beatmap["passes"] + (1 if score.passed else 0)

        # 这里应该更新 custom_maps 表的统计
        await app.state.services.database.execute(
            "UPDATE custom_maps SET plays = :plays, passes = :passes WHERE id = :map_id",
            {
                "plays": new_plays,
                "passes": new_passes,
                "map_id": custom_beatmap["id"],
            },
        )

    # 构建响应
    if not score.passed:
        response = b"error: no"
    else:
        # 简化的成绩图表响应
        submission_charts = [
            f"beatmapId:{custom_beatmap['id']}",
            f"beatmapSetId:{custom_beatmap['mapset_id']}",
            f"beatmapPlaycount:{new_plays}",
            f"beatmapPasscount:{new_passes}",
            f"approvedDate:{custom_beatmap['created_at']}",
            "\n",
            "chartId:beatmap",
            f"chartUrl:https://osu.ppy.sh/beatmapsets/{custom_beatmap['mapset_id']}",
            "chartName:Beatmap Ranking",
            f"rankBefore:|rankAfter:{score.rank if hasattr(score, 'rank') else ''}",
            f"rankedScoreBefore:|rankedScoreAfter:{score.score}",
            f"totalScoreBefore:|totalScoreAfter:{score.score}",
            f"maxComboBefore:|maxComboAfter:{score.max_combo}",
            f"accuracyBefore:|accuracyAfter:{score.acc:.2f}",
            f"ppBefore:|ppAfter:{score.pp:.2f}",
            f"onlineScoreId:{score.id}",
            "\n",
            "chartId:overall",
            f"chartUrl:https://{app.settings.DOMAIN}/u/{score.player.id}",
            "chartName:Overall Ranking",
            f"rankBefore:{prev_stats.rank}|rankAfter:{stats.rank}",
            f"rankedScoreBefore:{prev_stats.rscore}|rankedScoreAfter:{stats.rscore}",
            f"totalScoreBefore:{prev_stats.tscore}|totalScoreAfter:{stats.tscore}",
            f"maxComboBefore:{prev_stats.max_combo}|maxComboAfter:{stats.max_combo}",
            f"accuracyBefore:{prev_stats.acc:.2f}|accuracyAfter:{stats.acc:.2f}",
            f"ppBefore:{prev_stats.pp}|ppAfter:{stats.pp}",
            "achievements-new:",
        ]

        response = "|".join(submission_charts).encode()

    log(
        f"[{score.mode!r}] {score.player} submitted a custom map score! "
        f"({score.status!r}, {score.pp:,.2f}pp / {stats.pp:,}pp)",
        Ansi.LGREEN,
    )

    return Response(response)


def parse_form_data_score_params(
    score_data: FormData,
) -> tuple[bytes, StarletteUploadFile] | None:
    """Parse the score data, and replay file
    from the form data's 'score' parameters."""
    try:
        score_parts = score_data.getlist("score")
        assert len(score_parts) == 2, "Invalid score data"

        score_data_b64 = score_data.getlist("score")[0]
        assert isinstance(score_data_b64, str), "Invalid score data"
        replay_file = score_data.getlist("score")[1]
        assert isinstance(replay_file, StarletteUploadFile), "Invalid replay data"
    except AssertionError as exc:
        log(f"Failed to validate score multipart data: ({exc.args[0]})", Ansi.LRED)
        return None
    else:
        return (
            score_data_b64.encode(),
            replay_file,
        )


@router.post("/web/osu-submit-modular-selector.php")
async def osuSubmitModularSelector(
    request: Request,
    # TODO: should token be allowed
    # through but ac'd if not found?
    # TODO: validate token format
    # TODO: save token in the database
    token: str = Header(...),
    # TODO: do ft & st contain pauses?
    exited_out: bool = Form(..., alias="x"),
    fail_time: int = Form(..., alias="ft"),
    visual_settings_b64: bytes = Form(..., alias="fs"),
    updated_beatmap_hash: str = Form(..., alias="bmk"),
    storyboard_md5: str | None = Form(None, alias="sbk"),
    iv_b64: bytes = Form(..., alias="iv"),
    unique_ids: str = Form(..., alias="c1"),
    score_time: int = Form(..., alias="st"),
    pw_md5: str = Form(..., alias="pass"),
    osu_version: str = Form(..., alias="osuver"),
    client_hash_b64: bytes = Form(..., alias="s"),
    fl_cheat_screenshot: bytes | None = File(None, alias="i"),
) -> Response:
    """Handle a score submission from an osu! client with an active session."""
    # print(updated_beatmap_hash)
    if fl_cheat_screenshot:
        stacktrace = app.utils.get_appropriate_stacktrace()
        await app.state.services.log_strange_occurrence(stacktrace)

    # NOTE: the bancho protocol uses the "score" parameter name for both
    # the base64'ed score data, and the replay file in the multipart
    # starlette/fastapi do not support this, so we've moved it out
    score_parameters = parse_form_data_score_params(await request.form())
    if score_parameters is None:
        return Response(b"")

    # extract the score data and replay file from the score data
    score_data_b64, replay_file = score_parameters

    # decrypt the score data (aes)
    score_data, client_hash_decoded = encryption.decrypt_score_aes_data(
        score_data_b64,
        client_hash_b64,
        iv_b64,
        osu_version,
    )

    # fetch map & player

    bmap_md5 = score_data[0]
    bmap = await Beatmap.from_md5(bmap_md5)
    is_custom_map = False
    custom_beatmap = None
    # 判断是否为自定义谱面
    if not bmap:
        # Try to find a custom map with this MD5
        custom_beatmap = await custom_maps_repo.fetch_one(md5=bmap_md5)
        if custom_beatmap:
            is_custom_map = True
        else:
            # Map does not exist, most likely unsubmitted.
            return Response(b"error: beatmap")
    # print(score_data)
    # if the client has supporter, a space is appended
    # but usernames may also end with a space, which must be preserved
    username = score_data[1]
    if username[-1] == " ":
        username = username[:-1]

    player = await app.state.sessions.players.from_login(username, pw_md5)
    if not player:
        # Player is not online, return nothing so that their
        # client will retry submission when they log in.
        return Response(b"")

    # parse the score from the remaining data
    score = Score.from_submission(score_data[2:])

    # attach bmap & player
    score.bmap = bmap  # This might be None for custom maps
    score.player = player

    # 自定义谱面处理
    if is_custom_map and custom_beatmap is not None:
        # 为自定义谱面创建一个临时的 Beatmap 对象，确保 compute_online_checksum 能正常工作
        # 创建一个虚拟的 BeatmapSet
        dummy_set = BeatmapSet(
            id=custom_beatmap["mapset_id"],
            last_osuapi_check=datetime.now(),
        )

        # 创建一个临时的 Beatmap 对象，包含必要的 md5 信息
        temp_beatmap = Beatmap(
            map_set=dummy_set,
            md5=custom_beatmap["md5"],
            id=custom_beatmap["id"],
            set_id=custom_beatmap["mapset_id"],
        )

        # 为 score 设置这个临时的 beatmap，这样 compute_online_checksum 就能访问 md5
        score.bmap = temp_beatmap

        # 转换 CustomMap 为字典
        custom_beatmap_dict = {
            "id": custom_beatmap["id"],
            "mapset_id": custom_beatmap["mapset_id"],
            "md5": custom_beatmap["md5"],
            "plays": custom_beatmap["plays"],
            "passes": custom_beatmap["passes"],
            "created_at": custom_beatmap["created_at"],
            "star_rating": custom_beatmap.get("star_rating", 1.0),
            "overall_difficulty": custom_beatmap.get("overall_difficulty", 5.0),
            "approach_rate": custom_beatmap.get("approach_rate", 5.0),
            "circle_size": custom_beatmap.get("circle_size", 4.0),
            "hp_drain_rate": custom_beatmap.get("hp_drain_rate", 5.0),
            "max_combo": custom_beatmap.get("max_combo", 1000),
        }
        return await handle_custom_map_score_submission(
            score,
            custom_beatmap_dict,
            score_time,
            fail_time,
            replay_file,
            unique_ids,
            osu_version,
            client_hash_decoded,
            storyboard_md5,
            bmap_md5,
        )

    # At this point, bmap should not be None for official maps
    assert bmap is not None
    score.bmap = bmap  # 确保 score.bmap 不是 None

    ## perform checksum validation
    # 成绩提交
    unique_id1, unique_id2 = unique_ids.split("|", maxsplit=1)
    unique_id1_md5 = hashlib.md5(unique_id1.encode()).hexdigest()
    unique_id2_md5 = hashlib.md5(unique_id2.encode()).hexdigest()

    try:
        assert player.client_details is not None

        if osu_version != f"{player.client_details.osu_version.date:%Y%m%d}":
            raise ValueError("osu! version mismatch")

        if client_hash_decoded != player.client_details.client_hash:
            raise ValueError("client hash mismatch")
        # assert unique ids (c1) are correct and match login params
        if unique_id1_md5 != player.client_details.uninstall_md5:
            raise ValueError(
                f"unique_id1 mismatch ({unique_id1_md5} != {player.client_details.uninstall_md5})",
            )

        if unique_id2_md5 != player.client_details.disk_signature_md5:
            raise ValueError(
                f"unique_id2 mismatch ({unique_id2_md5} != {player.client_details.disk_signature_md5})",
            )

        # assert online checksums match
        server_score_checksum = score.compute_online_checksum(
            osu_version=osu_version,
            osu_client_hash=client_hash_decoded,
            storyboard_checksum=storyboard_md5 or "",
        )
        if score.client_checksum != server_score_checksum:
            raise ValueError(
                f"online score checksum mismatch ({server_score_checksum} != {score.client_checksum})",
            )

        # assert beatmap hashes match
        if bmap_md5 != updated_beatmap_hash:
            raise ValueError(
                f"beatmap hash mismatch ({bmap_md5} != {updated_beatmap_hash})",
            )

    except (ValueError, AssertionError):
        # NOTE: this is undergoing a temporary trial period,
        # after which, it will be enabled & perform restrictions.
        stacktrace = app.utils.get_appropriate_stacktrace()
        await app.state.services.log_strange_occurrence(stacktrace)

        # await player.restrict(
        #     admin=app.state.sessions.bot,
        #     reason="mismatching hashes on score submission",
        # )

        # refresh their client state
        # if player.online:
        #     player.logout()

        # return b"error: ban"

    # we should update their activity no matter
    # what the result of the score submission is.
    score.player.update_latest_activity_soon()

    # make sure the player's client displays the correct mode's stats
    if score.mode != score.player.status.mode:
        score.player.status.mods = score.mods
        score.player.status.mode = score.mode

        if not score.player.restricted:
            app.state.sessions.players.enqueue(app.packets.user_stats(score.player))

    # hold a lock around (check if submitted, submission) to ensure no duplicates
    # are submitted to the database, and potentially award duplicate score/pp/etc.
    async with app.state.score_submission_locks[score.client_checksum]:
        # stop here if this is a duplicate score
        if await app.state.services.database.fetch_one(
            "SELECT 1 FROM scores WHERE online_checksum = :checksum",
            {"checksum": score.client_checksum},
        ):
            log(f"{score.player} submitted a duplicate score.", Ansi.LYELLOW)
            return Response(b"error: no")

        # all data read from submission.
        # now we can calculate things based on our data.
        score.acc = score.calculate_accuracy()

        osu_file_available = await ensure_osu_file_is_available(
            bmap.id,
            expected_md5=bmap.md5,
        )
        if osu_file_available:
            score.pp, score.sr = score.calculate_performance(bmap.id)

            if score.passed:
                await score.calculate_status()

                if score.bmap.status != RankedStatus.Pending:
                    score.rank = await score.calculate_placement()
            else:
                score.status = SubmissionStatus.FAILED

        score.time_elapsed = score_time if score.passed else fail_time

        # TODO: re-implement pp caps for non-whitelisted players?

        """ Score submission checks completed; submit the score. """

        if app.state.services.datadog:
            app.state.services.datadog.increment("bancho.submitted_scores")  # type: ignore[no-untyped-call]

        if score.status == SubmissionStatus.BEST:
            if app.state.services.datadog:
                app.state.services.datadog.increment("bancho.submitted_scores_best")  # type: ignore[no-untyped-call]

            if score.bmap.has_leaderboard:
                if score.bmap.status == RankedStatus.Loved and score.mode in (
                    GameMode.VANILLA_OSU,
                    GameMode.VANILLA_TAIKO,
                    GameMode.VANILLA_CATCH,
                    GameMode.VANILLA_MANIA,
                ):
                    performance = f"{score.score:,} score"
                else:
                    performance = f"{score.pp:,.2f}pp"

                score.player.enqueue(
                    app.packets.notification(
                        f"You achieved #{score.rank}! ({performance})",
                    ),
                )

                if score.rank == 1 and not score.player.restricted:
                    announce_chan = app.state.sessions.channels.get_by_name("#announce")

                    ann = [
                        f"\x01ACTION achieved #1 on {score.bmap.embed}",
                        f"with {score.acc:.2f}% for {performance}.",
                    ]

                    if score.mods:
                        ann.insert(1, f"+{score.mods!r}")

                    scoring_metric = (
                        "pp" if score.mode >= GameMode.RELAX_OSU else "score"
                    )

                    # If there was previously a score on the map, add old #1.
                    prev_n1 = await app.state.services.database.fetch_one(
                        "SELECT u.id, name FROM users u "
                        "INNER JOIN scores s ON u.id = s.userid "
                        "WHERE s.map_md5 = :map_md5 AND s.mode = :mode "
                        "AND s.status = 2 AND u.priv & 1 "
                        f"ORDER BY s.{scoring_metric} DESC LIMIT 1",
                        {"map_md5": score.bmap.md5, "mode": score.mode},
                    )

                    if prev_n1:
                        if score.player.id != prev_n1["id"]:
                            ann.append(
                                f"(Previous #1: [https://{app.settings.DOMAIN}/u/"
                                "{id} {name}])".format(
                                    id=prev_n1["id"],
                                    name=prev_n1["name"],
                                ),
                            )

                    assert announce_chan is not None
                    announce_chan.send(" ".join(ann), sender=score.player, to_self=True)

            # this score is our best score.
            # update any preexisting personal best
            # records with SubmissionStatus.SUBMITTED.
            await app.state.services.database.execute(
                "UPDATE scores SET status = 1 "
                "WHERE status = 2 AND map_md5 = :map_md5 "
                "AND userid = :user_id AND mode = :mode",
                {
                    "map_md5": score.bmap.md5,
                    "user_id": score.player.id,
                    "mode": score.mode,
                },
            )

        score.id = await app.state.services.database.execute(
            "INSERT INTO scores "
            "VALUES (NULL, "
            ":map_md5, :score, :pp, :acc, "
            ":max_combo, :mods, :n300, :n100, "
            ":n50, :nmiss, :ngeki, :nkatu, "
            ":grade, :status, :mode, :play_time, "
            ":time_elapsed, :client_flags, :user_id, :perfect, "
            ":checksum)",
            {
                "map_md5": score.bmap.md5,
                "score": score.score,
                "pp": score.pp,
                "acc": score.acc,
                "max_combo": score.max_combo,
                "mods": score.mods,
                "n300": score.n300,
                "n100": score.n100,
                "n50": score.n50,
                "nmiss": score.nmiss,
                "ngeki": score.ngeki,
                "nkatu": score.nkatu,
                "grade": score.grade.name,
                "status": score.status,
                "mode": score.mode,
                "play_time": score.server_time,
                "time_elapsed": score.time_elapsed,
                "client_flags": score.client_flags,
                "user_id": score.player.id,
                "perfect": score.perfect,
                "checksum": score.client_checksum,
            },
        )

    if score.passed:
        replay_data = await replay_file.read()

        MIN_REPLAY_SIZE = 24

        if len(replay_data) >= MIN_REPLAY_SIZE:
            key = f"{app.settings.R2_REPLAY_FOLDER}/{score.id}1.osr"
            metadata = {
                "score-id": str(score.id),
                "score-type": "official",
                "user-id": str(score.player.id),
                "map-md5": score.bmap.md5,
                "mode": str(score.mode.value),
                "beatmap-id": str(score.bmap.id),
            }
            await app.storage.upload(
                app.settings.R2_BUCKET,
                key,
                replay_data,
                metadata=metadata,
            )
        else:
            log(f"{score.player} submitted a score without a replay!", Ansi.LRED)

            if not score.player.restricted:
                await score.player.restrict(
                    admin=app.state.sessions.bot,
                    reason="submitted score with no replay",
                )
                if score.player.is_online:
                    score.player.logout()

    """ Update the user's & beatmap's stats """

    # get the current stats, and take a
    # shallow copy for the response charts.
    stats = score.player.stats[score.mode]
    prev_stats = copy.copy(stats)

    # stuff update for all submitted scores
    stats.playtime += score.time_elapsed // 1000
    stats.plays += 1
    stats.tscore += score.score
    stats.total_hits += score.n300 + score.n100 + score.n50

    if score.mode.as_vanilla in (1, 3):
        # taiko uses geki & katu for hitting big notes with 2 keys
        # mania uses geki & katu for rainbow 300 & 200
        stats.total_hits += score.ngeki + score.nkatu

    stats_updates: dict[str, Any] = {
        "plays": stats.plays,
        "playtime": stats.playtime,
        "tscore": stats.tscore,
        "total_hits": stats.total_hits,
    }

    if score.passed and score.bmap.has_leaderboard:
        # player passed & map is ranked, approved, or loved.

        if score.max_combo > stats.max_combo:
            stats.max_combo = score.max_combo
            stats_updates["max_combo"] = stats.max_combo

        if score.bmap.awards_ranked_pp and score.status == SubmissionStatus.BEST:
            # map is ranked or approved, and it's our (new)
            # best score on the map. update the player's
            # ranked score, grades, pp, acc and global rank.

            additional_rscore = score.score
            if score.prev_best:
                # we previously had a score, so remove
                # it's score from our ranked score.
                additional_rscore -= score.prev_best.score

                if score.grade != score.prev_best.grade:
                    if score.grade >= Grade.A:
                        stats.grades[score.grade] += 1
                        grade_col = format(score.grade, "stats_column")
                        stats_updates[grade_col] = stats.grades[score.grade]

                    if score.prev_best.grade >= Grade.A:
                        stats.grades[score.prev_best.grade] -= 1
                        grade_col = format(score.prev_best.grade, "stats_column")
                        stats_updates[grade_col] = stats.grades[score.prev_best.grade]
            else:
                # this is our first submitted score on the map
                if score.grade >= Grade.A:
                    stats.grades[score.grade] += 1
                    grade_col = format(score.grade, "stats_column")
                    stats_updates[grade_col] = stats.grades[score.grade]

            stats.rscore += additional_rscore
            stats_updates["rscore"] = stats.rscore

            # fetch scores sorted by pp for total acc/pp calc - 包含官方谱面和自定义谱面
            # NOTE: we select all plays (and not just top100)
            # because bonus pp counts the total amount of ranked
            # scores. I'm aware this scales horribly, and it'll
            # likely be split into two queries in the future.

            # 获取官方谱面的最佳成绩
            official_best_scores = await app.state.services.database.fetch_all(
                "SELECT s.pp, s.acc FROM scores s "
                "INNER JOIN maps m ON s.map_md5 = m.md5 "
                "WHERE s.userid = :user_id AND s.mode = :mode "
                "AND s.status = 2 AND m.status IN (2, 3) "  # ranked, approved
                "ORDER BY s.pp DESC",
                {"user_id": score.player.id, "mode": score.mode},
            )

            # 获取自定义谱面的最佳成绩
            custom_best_scores = await app.state.services.database.fetch_all(
                "SELECT cs.pp, cs.acc FROM custom_scores cs "
                "INNER JOIN custom_maps cm ON cs.map_md5 = cm.md5 "
                "WHERE cs.user_id = :user_id AND cs.mode = :mode "
                "AND cs.status = 2 AND cm.status IN ('approved', 'loved') "  # approved, loved
                "ORDER BY cs.pp DESC",
                {"user_id": score.player.id, "mode": score.mode},
            )

            # 合并并按PP排序所有成绩
            best_scores = list(official_best_scores) + list(custom_best_scores)
            best_scores.sort(key=lambda x: x["pp"], reverse=True)

            # calculate new total weighted accuracy
            weighted_acc = sum(
                row["acc"] * 0.95**i for i, row in enumerate(best_scores)
            )
            bonus_acc = 100.0 / (20 * (1 - 0.95 ** len(best_scores)))
            stats.acc = (weighted_acc * bonus_acc) / 100
            stats_updates["acc"] = stats.acc

            # calculate new total weighted pp
            weighted_pp = sum(row["pp"] * 0.95**i for i, row in enumerate(best_scores))
            bonus_pp = 416.6667 * (1 - 0.9994 ** len(best_scores))
            stats.pp = round(weighted_pp + bonus_pp)
            stats_updates["pp"] = stats.pp

            # update global & country ranking
            stats.rank = await score.player.update_rank(score.mode)

    await stats_repo.partial_update(
        score.player.id,
        score.mode.value,
        plays=stats_updates.get("plays", UNSET),
        playtime=stats_updates.get("playtime", UNSET),
        tscore=stats_updates.get("tscore", UNSET),
        total_hits=stats_updates.get("total_hits", UNSET),
        max_combo=stats_updates.get("max_combo", UNSET),
        xh_count=stats_updates.get("xh_count", UNSET),
        x_count=stats_updates.get("x_count", UNSET),
        sh_count=stats_updates.get("sh_count", UNSET),
        s_count=stats_updates.get("s_count", UNSET),
        a_count=stats_updates.get("a_count", UNSET),
        rscore=stats_updates.get("rscore", UNSET),
        acc=stats_updates.get("acc", UNSET),
        pp=stats_updates.get("pp", UNSET),
    )

    if not score.player.restricted:
        # enqueue new stats info to all other users
        app.state.sessions.players.enqueue(app.packets.user_stats(score.player))

        # update beatmap with new stats
        score.bmap.plays += 1
        if score.passed:
            score.bmap.passes += 1

        await app.state.services.database.execute(
            "UPDATE maps SET plays = :plays, passes = :passes WHERE md5 = :map_md5",
            {
                "plays": score.bmap.plays,
                "passes": score.bmap.passes,
                "map_md5": score.bmap.md5,
            },
        )

    # update their recent score
    score.player.recent_scores[score.mode] = score

    """ score submission charts """

    # charts are only displayed for passes vanilla gamemodes.
    if not score.passed:  # TODO: check if this is correct
        response = b"error: no"
    else:
        # construct and send achievements & ranking charts to the client
        if score.bmap.awards_ranked_pp and not score.player.restricted:
            unlocked_achievements: list[Achievement] = []

            server_achievements = await achievements_usecases.fetch_many()
            player_achievements = await user_achievements_usecases.fetch_many(
                user_id=score.player.id,
            )

            for server_achievement in server_achievements:
                player_unlocked_achievement = any(
                    player_achievement
                    for player_achievement in player_achievements
                    if player_achievement["achid"] == server_achievement["id"]
                )
                if player_unlocked_achievement:
                    # player already has this achievement.
                    continue

                achievement_condition = server_achievement["cond"]
                if achievement_condition(score, score.mode.as_vanilla):
                    await user_achievements_usecases.create(
                        score.player.id,
                        server_achievement["id"],
                    )
                    unlocked_achievements.append(server_achievement)

            achievements_str = "/".join(
                format_achievement_string(a["file"], a["name"], a["desc"])
                for a in unlocked_achievements
            )
        else:
            achievements_str = ""

        # create score submission charts for osu! client to display

        if score.prev_best:
            beatmap_ranking_chart_entries = (
                chart_entry("rank", score.prev_best.rank, score.rank),
                chart_entry("rankedScore", score.prev_best.score, score.score),
                chart_entry("totalScore", score.prev_best.score, score.score),
                chart_entry("maxCombo", score.prev_best.max_combo, score.max_combo),
                chart_entry(
                    "accuracy",
                    round(score.prev_best.acc, 2),
                    round(score.acc, 2),
                ),
                chart_entry("pp", score.prev_best.pp, score.pp),
            )
        else:
            # no previous best score
            beatmap_ranking_chart_entries = (
                chart_entry("rank", None, score.rank),
                chart_entry("rankedScore", None, score.score),
                chart_entry("totalScore", None, score.score),
                chart_entry("maxCombo", None, score.max_combo),
                chart_entry("accuracy", None, round(score.acc, 2)),
                chart_entry("pp", None, score.pp),
            )

        overall_ranking_chart_entries = (
            chart_entry("rank", prev_stats.rank, stats.rank),
            chart_entry("rankedScore", prev_stats.rscore, stats.rscore),
            chart_entry("totalScore", prev_stats.tscore, stats.tscore),
            chart_entry("maxCombo", prev_stats.max_combo, stats.max_combo),
            chart_entry("accuracy", round(prev_stats.acc, 2), round(stats.acc, 2)),
            chart_entry("pp", prev_stats.pp, stats.pp),
        )

        submission_charts = [
            # beatmap info chart
            f"beatmapId:{score.bmap.id}",
            f"beatmapSetId:{score.bmap.set_id}",
            f"beatmapPlaycount:{score.bmap.plays}",
            f"beatmapPasscount:{score.bmap.passes}",
            f"approvedDate:{score.bmap.last_update}",
            "\n",
            # beatmap ranking chart
            "chartId:beatmap",
            f"chartUrl:{score.bmap.set.url}",
            "chartName:Beatmap Ranking",
            *beatmap_ranking_chart_entries,
            f"onlineScoreId:{score.id}",
            "\n",
            # overall ranking chart
            "chartId:overall",
            f"chartUrl:https://{app.settings.DOMAIN}/u/{score.player.id}",
            "chartName:Overall Ranking",
            *overall_ranking_chart_entries,
            f"achievements-new:{achievements_str}",
        ]

        response = "|".join(submission_charts).encode()
    # 读代码
    log(
        f"[{score.mode!r}] {score.player} submitted a score! "
        f"({score.status!r}, {score.pp:,.2f}pp / {stats.pp:,}pp)",
        Ansi.LGREEN,
    )

    return Response(response)


@router.get("/web/osu-getreplay.php")
async def getReplay(
    player: Player = Depends(authenticate_player_session(Query, "u", "h")),
    mode: int = Query(..., alias="m", ge=0, le=3),
    score_id: int = Query(..., alias="c", min=0, max=9_223_372_036_854_775_807),
) -> Response:
    # 将成绩ID转换为字符串以检查末尾数字
    score_id_str = str(score_id)

    # 根据成绩ID末尾数字判断成绩类型和文件路径
    if score_id_str.endswith("1"):
        # 官方成绩：ID末尾为1，去掉末尾1得到真实ID，文件以1结尾
        real_score_id = int(score_id_str[:-1])
        key = f"{app.settings.R2_REPLAY_FOLDER}/{real_score_id}1.osr"
        is_official_score = True
    elif score_id_str.endswith("2"):
        # 自定义成绩：ID末尾为2，去掉末尾2得到真实ID，文件以2结尾
        real_score_id = int(score_id_str[:-1])
        key = f"{app.settings.R2_REPLAY_FOLDER}/{real_score_id}2.osr"
        is_official_score = False
    else:
        # 如果ID末尾不是1或2，返回404
        return Response(b"", status_code=404)

    data = await app.storage.download(app.settings.R2_BUCKET, key)
    if data is None:
        return Response(b"", status_code=404)

    # 只对官方成绩增加回放观看次数
    if is_official_score:
        # 只有在需要增加观看次数时才查询成绩信息
        score = await Score.from_sql(real_score_id)
        if (
            score is not None
            and score.player is not None
            and player.id != score.player.id
        ):
            app.state.loop.create_task(score.increment_replay_views())  # type: ignore[unused-awaitable]

    return Response(data, media_type="application/octet-stream")


@router.get("/web/osu-rate.php")
async def osuRate(
    player: Player = Depends(
        authenticate_player_session(Query, "u", "p", err=b"auth fail"),
    ),
    map_md5: str = Query(..., alias="c", min_length=32, max_length=32),
    rating: int | None = Query(None, alias="v", ge=1, le=10),
) -> Response:
    if rating is None:
        # check if we have the map in our cache;
        # if not, the map probably doesn't exist.
        if map_md5 not in app.state.cache.beatmap:
            return Response(b"no exist")

        cached = app.state.cache.beatmap[map_md5]

        # only allow rating on maps with a leaderboard.
        if cached.status < RankedStatus.Ranked:
            return Response(b"not ranked")

        # osu! client is checking whether we can rate the map or not.
        # the client hasn't rated the map, so simply
        # tell them that they can submit a rating.
        if not await ratings_repo.fetch_one(map_md5=map_md5, userid=player.id):
            return Response(b"ok")
    else:
        # the client is submitting a rating for the map.
        await ratings_repo.create(userid=player.id, map_md5=map_md5, rating=rating)

    map_ratings = await ratings_repo.fetch_many(map_md5=map_md5)
    ratings = [row["rating"] for row in map_ratings]

    # send back the average rating
    avg = sum(ratings) / len(ratings)
    return Response(f"alreadyvoted\n{avg}".encode())


@unique
@pymysql_encode(escape_enum)
class LeaderboardType(IntEnum):
    Local = 0
    Top = 1
    Mods = 2
    Friends = 3
    Country = 4


async def get_leaderboard_scores(
    leaderboard_type: LeaderboardType | int,
    map_md5: str,
    mode: int,
    mods: Mods,
    player: Player,
    scoring_metric: Literal["pp", "score"],
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    query = [
        f"SELECT s.id, s.{scoring_metric} AS _score, "
        "s.max_combo, s.n50, s.n100, s.n300, "
        "s.nmiss, s.nkatu, s.ngeki, s.perfect, s.mods, "
        "UNIX_TIMESTAMP(s.play_time) time, u.id userid, "
        "COALESCE(CONCAT('[', c.tag, '] ', u.name), u.name) AS name "
        "FROM scores s "
        "INNER JOIN users u ON u.id = s.userid "
        "LEFT JOIN clans c ON c.id = u.clan_id "
        "WHERE s.map_md5 = :map_md5 AND s.status = 2 "  # 2: =best score
        "AND (u.priv & 1 OR u.id = :user_id) AND mode = :mode",
    ]

    params: dict[str, Any] = {
        "map_md5": map_md5,
        "user_id": player.id,
        "mode": mode,
    }

    if leaderboard_type == LeaderboardType.Mods:
        query.append("AND s.mods = :mods")
        params["mods"] = mods
    elif leaderboard_type == LeaderboardType.Friends:
        query.append("AND s.userid IN :friends")
        params["friends"] = player.friends | {player.id}
    elif leaderboard_type == LeaderboardType.Country:
        query.append("AND u.country = :country")
        params["country"] = player.geoloc["country"]["acronym"]

    # TODO: customizability of the number of scores
    query.append("ORDER BY _score DESC LIMIT 50")

    score_rows = await app.state.services.database.fetch_all(
        " ".join(query),
        params,
    )

    if score_rows:  # None or []
        # fetch player's personal best score
        personal_best_score_row = await app.state.services.database.fetch_one(
            f"SELECT id, {scoring_metric} AS _score, "
            "max_combo, n50, n100, n300, "
            "nmiss, nkatu, ngeki, perfect, mods, "
            "UNIX_TIMESTAMP(play_time) time "
            "FROM scores "
            "WHERE map_md5 = :map_md5 AND mode = :mode "
            "AND userid = :user_id AND status = 2 "
            "ORDER BY _score DESC LIMIT 1",
            {"map_md5": map_md5, "mode": mode, "user_id": player.id},
        )

        if personal_best_score_row is not None:
            # calculate the rank of the score.
            p_best_rank = 1 + await app.state.services.database.fetch_val(
                "SELECT COUNT(*) FROM scores s "
                "INNER JOIN users u ON u.id = s.userid "
                "WHERE s.map_md5 = :map_md5 AND s.mode = :mode "
                "AND s.status = 2 AND u.priv & 1 "
                f"AND s.{scoring_metric} > :score",
                {
                    "map_md5": map_md5,
                    "mode": mode,
                    "score": personal_best_score_row["_score"],
                },
                column=0,  # COUNT(*)
            )

            # attach rank to personal best row
            personal_best_score_row["rank"] = p_best_rank
    else:
        score_rows = []
        personal_best_score_row = None

    return score_rows, personal_best_score_row


async def get_custom_leaderboard_scores(
    leaderboard_type: LeaderboardType | int,
    map_md5: str,
    mode: int,
    mods: Mods,
    player: Player,
    scoring_metric: Literal["pp", "score"],
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """获取自定义谱面的排行榜成绩"""

    # 构建查询条件
    conditions = [
        "cs.map_md5 = :map_md5",
        "cs.mode = :mode",
        "cs.status = 2",
        "(u.priv & 1 OR u.id = :user_id)",
    ]

    params: dict[str, Any] = {
        "map_md5": map_md5,
        "user_id": player.id,
        "mode": mode,
    }

    if leaderboard_type == LeaderboardType.Mods:
        conditions.append("cs.mods = :mods")
        params["mods"] = mods
    elif leaderboard_type == LeaderboardType.Friends:
        conditions.append("cs.user_id IN :friends")
        params["friends"] = player.friends | {player.id}
    elif leaderboard_type == LeaderboardType.Country:
        conditions.append("u.country = :country")
        params["country"] = player.geoloc["country"]["acronym"]

    where_clause = " AND ".join(conditions)

    # 获取排行榜成绩 - 同时计算每个成绩的全局排名
    leaderboard_query = f"""
        SELECT cs.id, cs.{scoring_metric} AS _score,
               cs.max_combo, cs.n50, cs.n100, cs.n300,
               cs.nmiss, cs.nkatu, cs.ngeki, cs.perfect, cs.mods,
               UNIX_TIMESTAMP(cs.play_time) as time, u.id as userid,
               COALESCE(CONCAT('[', c.tag, '] ', u.name), u.name) AS name
        FROM custom_scores cs
        INNER JOIN users u ON cs.user_id = u.id
        LEFT JOIN clans c ON c.id = u.clan_id
        WHERE {where_clause}
        ORDER BY _score DESC
        LIMIT 50
    """

    score_rows = await app.state.services.database.fetch_all(
        leaderboard_query,
        params,
    )

    if score_rows:
        # 获取玩家个人最佳成绩
        personal_best_score_row = await custom_scores_repo.get_personal_best_score(
            map_md5,
            player.id,
            mode,
            scoring_metric,
        )

        if personal_best_score_row is not None:
            # 计算该成绩的排名
            rank_query = f"""
                SELECT COUNT(*) + 1 as rank
                FROM custom_scores cs
                INNER JOIN users u ON cs.user_id = u.id
                WHERE cs.map_md5 = :map_md5
                AND cs.mode = :mode
                AND cs.status = 2
                AND u.priv & 1
                AND cs.{scoring_metric} > :score
            """

            rank_result = await app.state.services.database.fetch_one(
                rank_query,
                {
                    "map_md5": map_md5,
                    "mode": mode,
                    "score": personal_best_score_row["_score"],
                },
            )

            personal_best_score_row["rank"] = rank_result["rank"] if rank_result else 1
    else:
        score_rows = []
        personal_best_score_row = None

    return score_rows, personal_best_score_row


SCORE_LISTING_FMTSTR = (
    "{id}|{name}|{score}|{max_combo}|"
    "{n50}|{n100}|{n300}|{nmiss}|{nkatu}|{ngeki}|"
    "{perfect}|{mods}|{userid}|{rank}|{time}|{has_replay}"
)


@router.get("/web/osu-osz2-getscores.php")
async def getScores(
    player: Player = Depends(authenticate_player_session(Query, "us", "ha")),
    requesting_from_editor_song_select: bool = Query(..., alias="s"),
    leaderboard_version: int = Query(..., alias="vv"),
    leaderboard_type: int = Query(..., alias="v", ge=0, le=4),
    map_md5: str = Query(..., alias="c", min_length=32, max_length=32),
    map_filename: str = Query(..., alias="f"),
    mode_arg: int = Query(..., alias="m", ge=0, le=3),
    map_set_id: int = Query(..., alias="i", ge=-1, le=2_147_483_647),
    mods_arg: int = Query(..., alias="mods", ge=0, le=2_147_483_647),
    map_package_hash: str = Query(..., alias="h"),  # TODO: further validation
    aqn_files_found: bool = Query(..., alias="a"),
) -> Response:
    # 打印请求的信息
    """log(
        f"Leaderboard request: {player} ({player.id}) "
        f"for map {map_md5} ({map_set_id}), mode {mode_arg}, "
        f"mods {mods_arg}, leaderboard type {leaderboard_type}",
        Ansi.LCYAN,
    )"""
    print(map_md5)
    if aqn_files_found:
        stacktrace = app.utils.get_appropriate_stacktrace()
        await app.state.services.log_strange_occurrence(stacktrace)
    # print(map_md5)
    # check if this md5 has already been  cached as
    # unsubmitted/needs update to reduce osu!api spam
    if map_md5 in app.state.cache.unsubmitted:
        return Response(b"-1|false")
    if map_md5 in app.state.cache.needs_update:
        return Response(b"1|false")

    if mods_arg & Mods.RELAX:
        if mode_arg == 3:  # rx!mania doesn't exist
            mods_arg &= ~Mods.RELAX
        else:
            mode_arg += 4
    elif mods_arg & Mods.AUTOPILOT:
        if mode_arg in (1, 2, 3):  # ap!catch, taiko and mania don't exist
            mods_arg &= ~Mods.AUTOPILOT
        else:
            mode_arg += 8

    mods = Mods(mods_arg)
    mode = GameMode(mode_arg)

    # attempt to update their stats if their
    # gm/gm-affecting-mods change at all.
    if mode != player.status.mode:
        player.status.mods = mods
        player.status.mode = mode

        if not player.restricted:
            app.state.sessions.players.enqueue(app.packets.user_stats(player))

    scoring_metric: Literal["pp", "score"] = (
        "pp" if mode >= GameMode.RELAX_OSU else "score"
    )

    bmap = await Beatmap.from_md5(map_md5, set_id=map_set_id)
    has_set_id = map_set_id > 0
    is_custom_map = False
    custom_beatmap = None

    if not bmap:
        # Try to find a custom map with this MD5
        custom_beatmap = await custom_maps_repo.fetch_one(md5=map_md5)
        if custom_beatmap:
            # Found a custom map, we'll handle it differently
            is_custom_map = True
            # Custom maps are always "ranked" for leaderboard purposes
            if custom_beatmap["status"] not in ["approved", "loved"]:
                # Only show leaderboards for approved or loved custom maps
                return Response(b"0|false")
        else:
            # map not found, figure out whether it needs an
            # update or isn't submitted using its filename.

            if has_set_id and map_set_id not in app.state.cache.beatmapset:
                # set not cached, it doesn't exist
                app.state.cache.unsubmitted.add(map_md5)
                return Response(b"-1|false")

            map_filename = unquote_plus(map_filename)  # TODO: is unquote needed?

            map_exists = False
            if has_set_id:
                # we can look it up in the specific set from cache
                for bmap in app.state.cache.beatmapset[map_set_id].maps:
                    if map_filename == bmap.filename:
                        map_exists = True
                        break
                else:
                    map_exists = False
            else:
                # we can't find it on the osu!api by md5,
                # and we don't have the set id, so we must
                # look it up in sql from the filename.
                map_exists = (
                    await maps_repo.fetch_one(
                        filename=map_filename,
                    )
                    is not None
                )

            if map_exists:
                # map can be updated.
                app.state.cache.needs_update.add(map_md5)
                return Response(b"1|false")
            else:
                # Try custom maps as well
                custom_beatmap = await custom_maps_repo.fetch_one(filename=map_filename)

                if custom_beatmap:
                    # Custom map found
                    is_custom_map = True
                else:
                    # map is unsubmitted.
                    # add this map to the unsubmitted cache, so
                    # that we don't have to make this request again.
                    app.state.cache.unsubmitted.add(map_md5)
                    return Response(b"-1|false")
    # print(is_custom_map)
    # Handle custom map scoring
    if is_custom_map and custom_beatmap is not None:
        if app.state.services.datadog:
            app.state.services.datadog.increment("bancho.leaderboards_served")  # type: ignore[no-untyped-call]

        # 检查自定义谱面状态，只显示已批准或已收录的自定义谱面排行榜
        if custom_beatmap["status"] not in ["approved", "loved"]:
            return Response(f"{custom_beatmap.get('status', 'pending')}|false".encode())

        # 获取自定义谱面的排行榜成绩和个人最佳
        if not requesting_from_editor_song_select:
            score_rows, personal_best_score_row = await get_custom_leaderboard_scores(
                leaderboard_type,
                custom_beatmap["md5"],
                mode,
                mods,
                player,
                scoring_metric,
            )
        else:
            score_rows = []
            personal_best_score_row = None

        # 获取自定义谱面评分（暂时使用默认值，后续可以实现评分系统）
        map_avg_rating = 0.0  # TODO: 实现自定义谱面评分系统

        # 构建自定义谱面的响应
        response_lines: list[str] = [
            # 自定义谱面状态为2（approved），表示可以显示排行榜
            f"2|false|{custom_beatmap['id']}|{custom_beatmap['mapset_id']}|{len(score_rows)}|0|",
            f"0\n{custom_beatmap.get('artist', '')} - {custom_beatmap.get('title', '')} [{custom_beatmap.get('version', '')}]\n{map_avg_rating}",
        ]

        if not score_rows:
            response_lines.extend(("", ""))  # no scores, no personal best
            return Response("\n".join(response_lines).encode())

        if personal_best_score_row is not None:
            user_clan = (
                await clans_repo.fetch_one(id=player.clan_id)
                if player.clan_id is not None
                else None
            )
            display_name = (
                f"[{user_clan['tag']}] {player.name}"
                if user_clan is not None
                else player.name
            )

            response_lines.append(
                SCORE_LISTING_FMTSTR.format(
                    id=f"{personal_best_score_row['id']}2",  # 自定义成绩ID末尾添加2
                    name=display_name,
                    score=int(round(personal_best_score_row["_score"])),
                    max_combo=personal_best_score_row["max_combo"],
                    n50=personal_best_score_row["n50"],
                    n100=personal_best_score_row["n100"],
                    n300=personal_best_score_row["n300"],
                    nmiss=personal_best_score_row["nmiss"],
                    nkatu=personal_best_score_row["nkatu"],
                    ngeki=personal_best_score_row["ngeki"],
                    perfect=personal_best_score_row["perfect"],
                    mods=personal_best_score_row["mods"],
                    userid=player.id,
                    rank=personal_best_score_row["rank"],
                    time=personal_best_score_row["time"],
                    has_replay="1",
                ),
            )
        else:
            response_lines.append("")

        response_lines.extend(
            [
                SCORE_LISTING_FMTSTR.format(
                    id=f"{s['id']}2",  # 自定义成绩ID末尾添加2
                    name=s["name"],
                    score=int(round(s["_score"])),
                    max_combo=s["max_combo"],
                    n50=s["n50"],
                    n100=s["n100"],
                    n300=s["n300"],
                    nmiss=s["nmiss"],
                    nkatu=s["nkatu"],
                    ngeki=s["ngeki"],
                    perfect=s["perfect"],
                    mods=s["mods"],
                    userid=s["userid"],
                    rank=idx + 1,
                    time=s["time"],
                    has_replay="1",
                )
                for idx, s in enumerate(score_rows)
            ],
        )
        return Response("\n".join(response_lines).encode())

    # At this point, we should have a valid bmap for official maps
    assert bmap is not None

    # we've found a beatmap for the request.
    if app.state.services.datadog:
        app.state.services.datadog.increment("bancho.leaderboards_served")  # type: ignore[no-untyped-call]

    if bmap.status < RankedStatus.Ranked:
        # only show leaderboards for ranked,
        # approved, qualified, or loved maps.
        return Response(f"{int(bmap.status)}|false".encode())

    # fetch scores & personal best
    # TODO: create a leaderboard cache
    if not requesting_from_editor_song_select:
        score_rows, personal_best_score_row = await get_leaderboard_scores(
            leaderboard_type,
            bmap.md5,
            mode,
            mods,
            player,
            scoring_metric,
        )
    else:
        score_rows = []
        personal_best_score_row = None

    # fetch beatmap rating
    map_ratings = await ratings_repo.fetch_many(
        map_md5=bmap.md5,
        page=None,
        page_size=None,
    )
    ratings = [row["rating"] for row in map_ratings]
    map_avg_rating = sum(ratings) / len(ratings) if ratings else 0.0

    ## construct response for osu! client

    response_lines: list[str] = [
        # NOTE: fa stands for featured artist (for the ones that may not know)
        # {ranked_status}|{serv_has_osz2}|{bid}|{bsid}|{len(scores)}|{fa_track_id}|{fa_license_text}
        f"{int(bmap.status)}|false|{bmap.id}|{bmap.set_id}|{len(score_rows)}|0|",
        # {offset}\n{beatmap_name}\n{rating}
        # TODO: server side beatmap offsets
        f"0\n{bmap.full_name}\n{map_avg_rating}",
    ]

    if not score_rows:
        response_lines.extend(("", ""))  # no scores, no personal best
        return Response("\n".join(response_lines).encode())

    if personal_best_score_row is not None:
        user_clan = (
            await clans_repo.fetch_one(id=player.clan_id)
            if player.clan_id is not None
            else None
        )
        display_name = (
            f"[{user_clan['tag']}] {player.name}"
            if user_clan is not None
            else player.name
        )
        response_lines.append(
            SCORE_LISTING_FMTSTR.format(
                id=f"{personal_best_score_row['id']}1",  # 官方成绩ID末尾添加1
                name=display_name,
                score=int(round(personal_best_score_row["_score"])),
                max_combo=personal_best_score_row["max_combo"],
                n50=personal_best_score_row["n50"],
                n100=personal_best_score_row["n100"],
                n300=personal_best_score_row["n300"],
                nmiss=personal_best_score_row["nmiss"],
                nkatu=personal_best_score_row["nkatu"],
                ngeki=personal_best_score_row["ngeki"],
                perfect=personal_best_score_row["perfect"],
                mods=personal_best_score_row["mods"],
                userid=player.id,
                rank=personal_best_score_row["rank"],
                time=personal_best_score_row["time"],
                has_replay="1",
            ),
        )
    else:
        response_lines.append("")

    response_lines.extend(
        [
            SCORE_LISTING_FMTSTR.format(
                id=f"{s['id']}1",  # 官方成绩ID末尾添加1
                name=s["name"],
                score=int(round(s["_score"])),
                max_combo=s["max_combo"],
                n50=s["n50"],
                n100=s["n100"],
                n300=s["n300"],
                nmiss=s["nmiss"],
                nkatu=s["nkatu"],
                ngeki=s["ngeki"],
                perfect=s["perfect"],
                mods=s["mods"],
                userid=s["userid"],
                rank=idx + 1,
                time=s["time"],
                has_replay="1",
            )
            for idx, s in enumerate(score_rows)
        ],
    )

    return Response("\n".join(response_lines).encode())


@router.post("/web/osu-comment.php")
async def osuComment(
    player: Player = Depends(authenticate_player_session(Form, "u", "p")),
    map_id: int = Form(..., alias="b"),
    map_set_id: int = Form(..., alias="s"),
    score_id: int = Form(..., alias="r", ge=0, le=9_223_372_036_854_775_807),
    mode_vn: int = Form(..., alias="m", ge=0, le=3),
    action: Literal["get", "post"] = Form(..., alias="a"),
    # only sent for post
    target: Literal["song", "map", "replay"] | None = Form(None),
    colour: str | None = Form(None, alias="f", min_length=6, max_length=6),
    start_time: int | None = Form(None, alias="starttime"),
    comment: str | None = Form(None, min_length=1, max_length=80),
) -> Response:
    if action == "get":
        # client is requesting all comments
        comments = await comments_repo.fetch_all_relevant_to_replay(
            score_id=score_id,
            map_set_id=map_set_id,
            map_id=map_id,
        )

        ret: list[str] = []

        for cmt in comments:
            # note: this implementation does not support
            #       "player" or "creator" comment colours
            if cmt["priv"] & Privileges.NOMINATOR:
                fmt = "bat"
            elif cmt["priv"] & Privileges.DONATOR:
                fmt = "supporter"
            else:
                fmt = ""

            if cmt["colour"]:
                fmt += f'|{cmt["colour"]}'

            ret.append(
                "{time}\t{target_type}\t{fmt}\t{comment}".format(fmt=fmt, **cmt),
            )

        player.update_latest_activity_soon()
        return Response("\n".join(ret).encode())

    elif action == "post":
        # client is submitting a new comment

        # validate all required params are provided
        assert target is not None
        assert start_time is not None
        assert comment is not None

        # get the corresponding id from the request
        if target == "song":
            target_id = map_set_id
        elif target == "map":
            target_id = map_id
        else:  # target == "replay"
            target_id = score_id

        if colour and not player.priv & Privileges.DONATOR:
            # only supporters can use colours.
            colour = None

            log(
                f"User {player} attempted to use a coloured comment without "
                "supporter status. Submitting comment without a colour.",
            )

        # insert into sql
        await comments_repo.create(
            target_id=target_id,
            target_type=comments_repo.TargetType(target),
            userid=player.id,
            time=start_time,
            comment=comment,
            colour=colour,
        )

        player.update_latest_activity_soon()

    return Response(b"")  # empty resp is fine


@router.get("/web/osu-markasread.php")
async def osuMarkAsRead(
    player: Player = Depends(authenticate_player_session(Query, "u", "h")),
    channel: str = Query(..., min_length=0, max_length=32),
) -> Response:
    target_name = unquote(channel)  # TODO: unquote needed?
    if not target_name:
        log(
            f"User {player} attempted to mark a channel as read without a target.",
            Ansi.LYELLOW,
        )
        return Response(b"")  # no channel specified

    target = await app.state.sessions.players.from_cache_or_sql(name=target_name)
    if target:
        # mark any unread mail from this user as read.
        await mail_repo.mark_conversation_as_read(
            to_id=player.id,
            from_id=target.id,
        )

    return Response(b"")


@router.get("/web/osu-getseasonal.php")
async def osuSeasonal() -> Response:
    return ORJSONResponse(app.settings.SEASONAL_BGS)


@router.get("/web/bancho_connect.php")
async def banchoConnect(
    # NOTE: this is disabled as this endpoint can be called
    #       before a player has been granted a session
    # player: Player = Depends(authenticate_player_session(Query, "u", "h")),
    osu_ver: str = Query(..., alias="v"),
    active_endpoint: str | None = Query(None, alias="fail"),
    net_framework_vers: str | None = Query(None, alias="fx"),  # delimited by |
    client_hash: str | None = Query(None, alias="ch"),
    retrying: bool | None = Query(None, alias="retry"),  # '0' or '1'
) -> Response:
    return Response(b"")


@router.get("/web/check-updates.php")
async def checkUpdates(
    request: Request,
    action: Literal["check", "path", "error"],
    stream: Literal["cuttingedge", "stable40", "beta40", "stable"],
) -> Response:
    return Response(b"")


""" Misc handlers """


if app.settings.REDIRECT_OSU_URLS:
    # NOTE: this will likely be removed with the addition of a frontend.
    async def osu_redirect(request: Request, _: int = Path(...)) -> Response:
        return RedirectResponse(
            url=f"https://osu.ppy.sh{request['path']}",
            status_code=status.HTTP_301_MOVED_PERMANENTLY,
        )

    for pattern in (
        "/beatmapsets/{_}",
        "/beatmaps/{_}",
        "/beatmapsets/{_}/discussion",
        "/community/forums/topics/{_}",
    ):
        router.get(pattern)(osu_redirect)


@router.get("/ss/{screenshot_id}.{extension}")
async def get_screenshot(
    screenshot_id: str = Path(..., pattern=r"[a-zA-Z0-9-_]{8}"),
    extension: Literal["jpg", "jpeg", "png"] = Path(...),
) -> Response:
    """Serve a screenshot from the server, by filename."""
    screenshot_path = SCREENSHOTS_PATH / f"{screenshot_id}.{extension}"

    if not screenshot_path.exists():
        return ORJSONResponse(
            content={"status": "Screenshot not found."},
            status_code=status.HTTP_404_NOT_FOUND,
        )

    if extension in ("jpg", "jpeg"):
        media_type = "image/jpeg"
    elif extension == "png":
        media_type = "image/png"
    else:
        media_type = None

    return FileResponse(
        path=screenshot_path,
        media_type=media_type,
    )


@router.get("/d/{map_set_id}")
async def get_osz(
    map_set_id: str = Path(...),
) -> Response:
    """Handle a map download request (osu.ppy.sh/d/*)."""
    no_video = map_set_id[-1] == "n"
    if no_video:
        map_set_id = map_set_id[:-1]

    query_str = f"{map_set_id}?n={int(not no_video)}"

    return RedirectResponse(
        url=f"{app.settings.MIRROR_DOWNLOAD_ENDPOINT}/{query_str}",
        status_code=status.HTTP_301_MOVED_PERMANENTLY,
    )


@router.get("/web/maps/{map_filename}")
async def get_updated_beatmap(
    request: Request,
    map_filename: str,
    host: str = Header(...),
) -> Response:
    """Send the latest .osu file the server has for a given map."""
    if host == "osu.ppy.sh":
        return Response("bancho.py only supports the -devserver connection method")

    # 智能谱面查询：首先检查是否为自定义谱面
    log(f"Requesting beatmap file: {map_filename}", Ansi.LCYAN)

    # 第一步：检查自定义谱面数据库
    custom_beatmap = None
    try:
        custom_beatmap = await custom_maps_repo.fetch_one(filename=map_filename)
        if custom_beatmap:
            log(
                f"Found custom beatmap: {map_filename} (MD5: {custom_beatmap['md5']})",
                Ansi.LGREEN,
            )
    except Exception as e:
        log(f"Error checking custom maps: {e}", Ansi.LYELLOW)

    # 第二步：如果找到自定义谱面，优先使用自定义谱面
    if custom_beatmap:
        try:
            # 从user_backend获取.osu文件内容
            osu_file_content = await get_custom_map_osu_file(custom_beatmap["md5"])

            if osu_file_content:
                log(
                    f"Successfully serving custom map file: {map_filename}",
                    Ansi.LGREEN,
                )
                return Response(
                    content=osu_file_content.encode("utf-8"),
                    media_type="application/octet-stream",
                    headers={
                        "Content-Disposition": f"attachment; filename={map_filename}",
                    },
                )
            else:
                log(
                    f"Custom map found but .osu file not available: {map_filename}",
                    Ansi.LYELLOW,
                )
        except Exception as e:
            log(f"Failed to fetch custom map .osu file: {e}", Ansi.LRED)

    # 第三步：如果不是自定义谱面或自定义谱面获取失败，尝试官方服务器
    try:
        log(f"Attempting to fetch from official server: {map_filename}", Ansi.LCYAN)
        response = await app.state.services.http_client.get(
            f"https://osu.ppy.sh{request['raw_path'].decode()}",
            timeout=5.0,
        )

        if response.status_code == 200 and response.content:
            log(f"Successfully serving official map file: {map_filename}", Ansi.LGREEN)
            return Response(
                content=response.content,
                media_type="application/octet-stream",
                headers={"Content-Disposition": f"attachment; filename={map_filename}"},
            )
        else:
            log(
                f"Official server returned status {response.status_code} for {map_filename}",
                Ansi.LYELLOW,
            )
    except Exception as e:
        log(f"Failed to fetch from official server: {e}", Ansi.LYELLOW)

    # 第四步：如果官方服务器也失败，尝试根据MD5查找自定义谱面
    # 有时文件名可能不匹配，但MD5可能匹配
    try:
        # 如果文件名包含MD5（某些情况下），尝试提取MD5
        import re

        md5_match = re.search(r"[a-fA-F0-9]{32}", map_filename)
        if md5_match:
            potential_md5 = md5_match.group()
            custom_beatmap_by_md5 = await custom_maps_repo.fetch_one(md5=potential_md5)

            if custom_beatmap_by_md5:
                log(f"Found custom beatmap by MD5: {potential_md5}", Ansi.LGREEN)
                osu_file_content = await get_custom_map_osu_file(potential_md5)

                if osu_file_content:
                    log(
                        f"Successfully serving custom map by MD5: {map_filename}",
                        Ansi.LGREEN,
                    )
                    return Response(
                        content=osu_file_content.encode("utf-8"),
                        media_type="application/octet-stream",
                        headers={
                            "Content-Disposition": f"attachment; filename={map_filename}",
                        },
                    )
    except Exception as e:
        log(f"Failed MD5 fallback search: {e}", Ansi.LYELLOW)

    # 最后：如果所有方法都失败，返回404或重定向到官方服务器
    log(f"All methods failed for {map_filename}, falling back to redirect", Ansi.LRED)

    # 可以选择返回404或重定向到官方服务器
    # 这里我们保持重定向行为以保持兼容性
    return RedirectResponse(
        url=f"https://osu.ppy.sh{request['raw_path'].decode()}",
        status_code=status.HTTP_301_MOVED_PERMANENTLY,
    )


@router.get("/p/doyoureallywanttoaskpeppy")
async def peppyDMHandler() -> Response:
    return Response(
        content=(
            b"This user's ID is usually peppy's (when on bancho), "
            b"and is blocked from being messaged by the osu! client."
        ),
    )


""" ingame registration """

INGAME_REGISTRATION_DISALLOWED_ERROR = {
    "form_error": {
        "user": {
            "password": [
                "In-game registration is disabled. Please register on the website.",
            ],
        },
    },
}


@router.post("/users")
async def register_account(
    request: Request,
    username: str = Form(..., alias="user[username]"),
    email: str = Form(..., alias="user[user_email]"),
    pw_plaintext: str = Form(..., alias="user[password]"),
    check: int = Form(...),
    # XXX: require/validate these headers; they are used later
    # on in the registration process for resolving geolocation
    forwarded_ip: str = Header(..., alias="X-Forwarded-For"),
    real_ip: str = Header(..., alias="X-Real-IP"),
) -> Response:
    if not all((username, email, pw_plaintext)):
        return Response(
            content=b"Missing required params",
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    # Disable in-game registration if enabled
    if app.settings.DISALLOW_INGAME_REGISTRATION:
        return ORJSONResponse(
            content=INGAME_REGISTRATION_DISALLOWED_ERROR,
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    # ensure all args passed
    # are safe for registration.
    errors: Mapping[str, list[str]] = defaultdict(list)

    # Usernames must:
    # - be within 2-15 characters in length
    # - not contain both ' ' and '_', one is fine
    # - not be in the config's `disallowed_names` list
    # - not already be taken by another player
    if not regexes.USERNAME.match(username):
        errors["username"].append("Must be 2-15 characters in length.")

    if "_" in username and " " in username:
        errors["username"].append('May contain "_" and " ", but not both.')

    if username in app.settings.DISALLOWED_NAMES:
        errors["username"].append("Disallowed username; pick another.")

    if "username" not in errors:
        if await users_repo.fetch_one(name=username):
            errors["username"].append("Username already taken by another player.")

    # Emails must:
    # - match the regex `^[^@\s]{1,200}@[^@\s\.]{1,30}\.[^@\.\s]{1,24}$`
    # - not already be taken by another player
    if not regexes.EMAIL.match(email):
        errors["user_email"].append("Invalid email syntax.")
    else:
        if await users_repo.fetch_one(email=email):
            errors["user_email"].append("Email already taken by another player.")

    # Passwords must:
    # - be within 8-32 characters in length
    # - have more than 3 unique characters
    # - not be in the config's `disallowed_passwords` list
    if not 8 <= len(pw_plaintext) <= 32:
        errors["password"].append("Must be 8-32 characters in length.")

    if len(set(pw_plaintext)) <= 3:
        errors["password"].append("Must have more than 3 unique characters.")

    if pw_plaintext.lower() in app.settings.DISALLOWED_PASSWORDS:
        errors["password"].append("That password was deemed too simple.")

    if errors:
        # we have errors to send back, send them back delimited by newlines.
        errors = {k: ["\n".join(v)] for k, v in errors.items()}
        errors_full = {"form_error": {"user": errors}}
        return ORJSONResponse(
            content=errors_full,
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    if check == 0:
        # the client isn't just checking values,
        # they want to register the account now.
        # make the md5 & bcrypt the md5 for sql.
        pw_md5 = hashlib.md5(pw_plaintext.encode()).hexdigest().encode()
        pw_bcrypt = bcrypt.hashpw(pw_md5, bcrypt.gensalt())
        app.state.cache.bcrypt[pw_bcrypt] = pw_md5  # cache result for login

        ip = app.state.services.ip_resolver.get_ip(request.headers)

        geoloc = await app.state.services.fetch_geoloc(ip, request.headers)
        country = geoloc["country"]["acronym"] if geoloc is not None else "XX"

        async with app.state.services.database.transaction():
            # add to `users` table.
            player = await users_repo.create(
                name=username,
                email=email,
                pw_bcrypt=pw_bcrypt,
                country=country,
            )

            # add to `stats` table.
            await stats_repo.create_all_modes(player_id=player["id"])

        if app.state.services.datadog:
            app.state.services.datadog.increment("bancho.registrations")  # type: ignore[no-untyped-call]

        log(f"<{username} ({player['id']})> has registered!", Ansi.LGREEN)

    return Response(content=b"ok")  # success


@router.post("/difficulty-rating")
async def difficultyRatingHandler(request: Request) -> Response:
    return RedirectResponse(
        url=f"https://osu.ppy.sh{request['path']}",
        status_code=status.HTTP_307_TEMPORARY_REDIRECT,
    )


@router.get("/thumb/{beatmap_id}l.jpg")
async def get_custom_beatmap_thumbnail(beatmap_id: int) -> Response:
    """代理自定义谱面缩略图请求到user_backend"""
    try:
        # 首先检查是否为自定义谱面
        custom_beatmap = await custom_maps_repo.fetch_one(id=beatmap_id)
        if not custom_beatmap:
            # 不是自定义谱面，返回404
            raise HTTPException(status_code=404, detail="Thumbnail not found")

        # 代理请求到user_backend
        response = await app.state.services.http_client.get(
            f"https://a.gu-osu.gmoe.cc/custom-mapsets/thumb/{beatmap_id}l.jpg",
        )

        if response.status_code == 200:
            return Response(
                content=response.content,
                media_type="image/jpeg",
                headers={"Cache-Control": "public, max-age=3600"},
            )
        else:
            raise HTTPException(status_code=404, detail="Thumbnail not found")

    except Exception as e:
        log(f"Failed to fetch thumbnail for beatmap {beatmap_id}: {e}", Ansi.LRED)
        raise HTTPException(status_code=404, detail="Thumbnail not found")


@router.get("/_mt/{beatmap_id}")
async def get_custom_beatmap_thumbnail_legacy(beatmap_id: int) -> Response:
    """兼容性端点 - 重定向到标准缩略图端点"""
    return RedirectResponse(url=f"/thumb/{beatmap_id}l.jpg")
