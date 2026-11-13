"""逐格出圖（S7）：compile_chapter 的工作 → ComfyUI → renders/chNNN/<格 id>_s<seed>[_<預設>][__<profile>].png。

命令列（tools/panels.py）與介面共用。停止靠 should_stop（回傳 True 時丟 Cancelled），進度靠 on_event：
    {"kind": "render"|"skip", "done", "total", "id", "seed", "file", "size"}

出圖 profile（N4）：用哪一組 workflow／模型／預設由 project.yaml 的 comfy_profile 決定，
沒寫就用 config.json 的 comfy.default_profile；呼叫端可以傳 profile_name 臨時換（--profile）。
"""
from . import comfy, compose, workspace
from .compile import CompileError, compile_chapter
from .project import ProjectError


class RenderError(Exception):
    """給人看的錯誤訊息（寫出哪一格哪個 seed）。"""


class Cancelled(Exception):
    """使用者要求停止。"""


def presets(cfg):
    comfy_cfg = cfg.get("comfy") or {}
    return {k: v for k, v in (comfy_cfg.get("presets") or {}).items() if not k.startswith("_")}, comfy_cfg.get("default_preset") or None


def resolve_profile(cfg, project=None, name=None):
    """這次出圖的 profile（comfy.resolve_profile）。錯誤轉成 RenderError，訊息直接可以給人看。"""
    try:
        return comfy.resolve_profile(cfg, name=name, project_config=project.config if project is not None else None)
    except comfy.ComfyError as error:
        raise RenderError(str(error))


def resolve_preset(cfg, name=None, profile=None):
    """出圖預設：(名稱, 內容, 預設名稱)。預設表來自 profile；tools/panels.py、bible.resolve_preset 同一套規則。"""
    try:
        prof = profile if profile is not None else comfy.resolve_profile(cfg)
        return comfy.resolve_preset(prof, name)
    except comfy.ComfyError as error:
        raise RenderError(str(error))


def chapter_jobs(project, chapter, page=None, panel=None, candidates=None):
    """要出圖的格：整章、某一頁，或某一格。"""
    try:
        storyboard = project.load_storyboard(chapter)
        overrides = {"candidates": candidates} if candidates else None
        jobs = compile_chapter(project, storyboard, only_page=page, overrides=overrides)
    except (ProjectError, CompileError) as error:
        raise RenderError(str(error))
    if panel:
        jobs = [j for j in jobs if j["id"] == panel]
        if not jobs:
            raise RenderError("分鏡裡沒有格「%s」" % panel)
    return jobs


def file_profile(profile_name, default_profile):
    """檔名裡的 profile 後綴（預設 profile 不加，見 comfy.render_name）。"""
    return profile_name if profile_name and profile_name != default_profile else ""


def existing_seeds(project, chapter, panel_id, preset_name, default_preset, profile_name=None, default_profile=None):
    """這一格在同一個出圖預設、同一個 profile 下已經畫過的 seed。"""
    suffix = preset_name if preset_name and preset_name != default_preset else ""
    want = file_profile(profile_name, default_profile)
    out = set()
    for name in compose.candidates(project, chapter, panel_id):
        info = workspace.candidate_info(name)
        if info["seed"] is not None and info["preset"] == suffix and info["profile"] == want:
            out.add(info["seed"])
    return out


def plan_more(project, chapter, jobs, more, preset_name, default_preset, profile_name=None, default_profile=None):
    """「加畫 N 張」：每格各取 N 個還沒畫過的 seed（見 workspace.next_seeds），寫進 job["run_seeds"]。"""
    base = project.config.get("seed_base", 0)
    for job in jobs:
        have = existing_seeds(project, chapter, job["id"], preset_name, default_preset, profile_name, default_profile)
        job["run_seeds"] = workspace.next_seeds(job, more, have, base)
    return jobs


def render_jobs(project, chapter, jobs, cfg, preset_name=None, force=False, should_stop=None, on_event=None,
                profile_name=None):
    """送 ComfyUI 出圖。job["run_seeds"] 有給就只畫那些（否則畫 job["seeds"]）。已存在的檔案跳過（force 就重畫）。
    回傳寫出的檔名清單。"""
    profile = resolve_profile(cfg, project, profile_name)
    chosen, preset, default = resolve_preset(cfg, preset_name, profile)
    try:
        workflow = comfy.load_workflow(profile["workflow"])
        default_profile = comfy.default_profile_name(cfg)
    except comfy.ComfyError as error:
        raise RenderError(str(error))
    folder = project.out_dir("renders", compose.chapter_name(chapter))
    build = project.out_dir("build", compose.chapter_name(chapter))
    todo = [(job, seed) for job in jobs for seed in (job["run_seeds"] if "run_seeds" in job else job["seeds"])]
    written = []
    stop = should_stop or (lambda: False)
    from .compile import save_job
    for job in jobs:
        # 只為「真的會畫」的格記指紋：全部略過（force=False 且檔案都在）時還寫指紋，
        # 會讓過期清單以為圖已經跟新卡片一致（舊圖還在、清單卻變 0 格）。
        seeds = job["run_seeds"] if "run_seeds" in job else job["seeds"]
        names = [comfy.render_name(job["id"], seed, chosen, default, profile["name"], default_profile) for seed in seeds]
        if force or any(not (folder / name).exists() for name in names):
            save_job(build, job)
    for i, (job, seed) in enumerate(todo, 1):
        if stop():
            raise Cancelled()
        name = comfy.render_name(job["id"], seed, chosen, default, profile["name"], default_profile)
        target = folder / name
        event = {"done": i, "total": len(todo), "id": job["id"], "seed": seed, "file": name, "size": (job["width"], job["height"]),
                 "profile": profile["name"]}
        if target.exists() and not force:
            if on_event:
                on_event(dict(event, kind="skip"))
            continue
        if on_event:
            on_event(dict(event, kind="start"))
        try:
            negative = job["negative"] or profile.get("negative") or ""
            wf, out_id = comfy.prepare(workflow, job["prompt"], negative, seed, job["width"], job["height"],
                                       models=profile.get("models"), prefix="text2comics/%s" % compose.chapter_name(chapter),
                                       preset=preset, loras=profile.get("loras"), profile_name=profile["name"])
            data = comfy.run(profile["base_url"], wf, out_id, should_stop=stop)
        except comfy.ComfyError as error:
            if stop():
                raise Cancelled()
            raise RenderError("%s（%s seed %d，profile %s）" % (error, job["id"], seed, profile["name"]))
        target.write_bytes(data)
        written.append(name)
        if on_event:
            on_event(dict(event, kind="render"))
    return written
