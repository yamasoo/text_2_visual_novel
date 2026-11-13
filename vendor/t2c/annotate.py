"""切對話（N3）：小說一章 → 場 → 旁白／對話／演出，每一句都回指原著的段落（source_para）。

    adapter = Adapter(project, prov_for=...)
    scenes, report = adapter.run_annotate(1)          # scenes/ch001.json ＋ build/adapt/ch001_scenes_fidelity.md
    board = annotate.to_storyboard(project, scenes)   # 程式轉成分鏡 JSON
    compile_chapter(project, board)                   # 保證能編譯

模型輸出的是「op」：一場是一串指令，N＝整段旁白、n＝段落裡的一段敘述、d＝一句對話、x＝演出（鏡頭、角度）。
程式把 op 展開成 scene graph，並在這裡做三件事（概念移植自 t2vn 的 annotate.py）：

1. **逐字驗證**：旁白與對話都必須是該段落原文的子句（忽略標點；允許句首補一個代名詞當主詞）；
2. **段落全覆蓋**：原著每一個段落都要用到，漏掉的段落直接退回給模型修；
3. **漏句修補**：repair_omissions() 把某一段被漏掉的敘述原文照順序補回（不經過模型）。

形狀跟 VN 不同：VN 的 bg／bgm／立繪位置／八種表情換成漫畫的「場 → 頁 → 格」，所以 scene 只留
location（bible/locations 的地點 id）、setting_en（這一場的英文畫面描述）、cast 與 lines。
to_storyboard() 再把 lines 排成 panels／bubbles（台詞逐字複製，程式負責斷行與 anchor），
分鏡該有的鏡頭、角度由 x（演出）決定。

只用標準函式庫，不依賴 FastAPI／fekit。
"""
import re
from dataclasses import dataclass, field

from . import layout, script as sc
from .check import CJK, COLUMN_CHARS, COLUMN_CHARS_NARRATION
from .compile import ANGLE, BUBBLE_KINDS, SHOT, CompileError, compile_chapter
from .script import Issue, MAX_BUBBLES
from .script import _parse_marks as parse_marks      # 劇本與切對話共用同一套括號標記（心聲、喊、通訊、畫面外）

SCHEMA_VERSION = 1
SOFT_LIMIT = 60              # split_text：短句合併到約 60 字
HARD_LIMIT = 90              # split_text：超過 90 字的句子在逗號斷
MAX_SCENE_PARAGRAPHS = 35    # 單場最多涵蓋幾個段落
MAX_SCENES = 60

LINE_KINDS = ("narration", "dialogue", "direction")
OP_KINDS = ("N", "n", "d", "x")

_SENTENCE_END = re.compile(r"(?<=[。！？])(?![」』…—])")
_CLAUSE_SPLIT = re.compile(r"[，。？！：；、,.?!:;．]")
_CLAUSE_STRIP = "…—『』「」\"“”‘’' 　"
_PRONOUNS = "她他它我你"
# 「她說」「對方問」這種純對白標籤：說話的人已經在對白行上，不算漏掉敘述
_SPEECH_TAG = re.compile(r"^(?:最後|然後|後來)?[^沒不只最又才終再先已也還都沉低輕突後然]{0,6}(?:說|問|道|答|喊|叫|笑道|嘆道|罵|嚷)$")
_OMIT_SPLIT = re.compile(r"[，。？！：；、,.?!:;．「」『』\"“”]")
_SPEECH_END = "。？！…—.?!．"
_HEADING = re.compile(r"^\s*#{1,6}\s")
_SEPARATOR = re.compile(r"^[\s＊*—－\-＝=·・~～#]+$")
_COLUMN_TAIL = "。，、！？；：…—」』）】,.;:!?)]}"

# 每個對話框的 anchor（依閱讀順序：右上 → 左上 → 右下 → 左下 → 中下）
_ANCHORS = [(0.80, 0.10), (0.20, 0.10), (0.80, 0.45), (0.20, 0.45), (0.50, 0.85)]
# 一格 1／2／3 個人的預設框（比例座標，兩人以上左右分開）
_BOXES = [
    [[0.12, 0.06, 0.88, 1.0]],
    [[0.02, 0.05, 0.48, 1.0], [0.52, 0.05, 0.98, 1.0]],
    [[0.0, 0.05, 0.34, 1.0], [0.33, 0.05, 0.67, 1.0], [0.66, 0.05, 1.0, 1.0]],
]
DIALOGUE_CHARS = COLUMN_CHARS - 1          # 11：留一個字的空間給「標點不要掉到下一欄」
NARRATION_CHARS = COLUMN_CHARS_NARRATION - 1


class AnnotateError(Exception):
    """給人看的錯誤訊息（切對話失敗、無法轉成分鏡…）。"""


@dataclass
class Expanded:
    """展開後的結果。issues 是空清單才代表可以採用。"""
    scene_doc: dict = field(default_factory=dict)
    issues: list = field(default_factory=list)
    warnings: list = field(default_factory=list)


# ---------------------------------------------------------------------------
# 段落：source_para 的編號就是這裡的順序
# ---------------------------------------------------------------------------
def split_paragraphs(source):
    """原著 → 段落清單（第 1 段是 1）。略過 Markdown 標題行與純分隔線（＊＊＊、——），它們不是內容。"""
    text = (source or "").replace("\r\n", "\n")
    out = []
    for block in re.split(r"\n[ \t　]*\n", text):
        lines = [line.strip() for line in block.split("\n") if line.strip() and not _HEADING.match(line)]
        body = "\n".join(lines).strip()
        if not body or _SEPARATOR.match(body):
            continue
        out.append(body)
    return out


def paragraph_map(paragraphs):
    return {index + 1: text for index, text in enumerate(paragraphs or [])}


def chapter_title(source):
    """原著開頭「# 第 1 章 大夜班」的標題；沒有就空字串。"""
    for line in (source or "").replace("\r\n", "\n").split("\n"):
        match = re.match(r"^\s*#{1,6}\s+(.*?)\s*$", line)
        if match:
            return match.group(1)
    return ""


def split_text(text):
    """句末（。！？）斷句，短句合併到約 60 字；超過 90 字的句子在逗號斷，還是太長就硬切。"""
    pieces = []
    for sentence in (s for s in _SENTENCE_END.split(str(text or "")) if s):
        if len(sentence) <= HARD_LIMIT:
            pieces.append(sentence)
            continue
        buffer = ""
        for part in re.split(r"(?<=，)", sentence):
            while len(part) > HARD_LIMIT:
                if buffer:
                    pieces.append(buffer)
                    buffer = ""
                pieces.append(part[:HARD_LIMIT])
                part = part[HARD_LIMIT:]
            if buffer and len(buffer) + len(part) > HARD_LIMIT:
                pieces.append(buffer)
                buffer = ""
            buffer += part
        if buffer:
            pieces.append(buffer)
    out, buffer = [], ""
    for piece in pieces:
        if buffer and len(buffer) + len(piece) > SOFT_LIMIT:
            out.append(buffer)
            buffer = ""
        buffer += piece
    if buffer.strip():
        out.append(buffer)
    return out


def has_quoted_speech(text):
    """段落裡有像是說話的引號內容（引號內以句末標點結尾）。只是引用名稱（「灰船」）不算。"""
    for inner in sc.QUOTE_RE.findall(text or ""):
        if inner.rstrip("」』\"” ").endswith(tuple(_SPEECH_END)):
            return True
    return False


# ---------------------------------------------------------------------------
# 逐字與漏句（移植 t2vn annotate.py 的 unfaithful_clauses／omitted_clauses／repair_omissions）
# ---------------------------------------------------------------------------
def locate_clause(clause, paragraphs, exclude=None):
    """這個子句出現在哪一段（給修正回饋用）。"""
    target = sc.normalize(clause)
    for number in sorted(paragraphs or {}):
        if number == exclude:
            continue
        source = paragraphs[number]
        if clause in source or (target and target in sc.normalize(source)):
            return number
    return None


def unfaithful_clauses(text, source):
    """回傳不是原文的子句（忽略標點；允許句首補一個代名詞當主詞）。"""
    source = str(source or "")
    src_norm = sc.normalize(source)
    bad = []
    for clause in _CLAUSE_SPLIT.split(str(text or "")):
        clause = clause.strip(_CLAUSE_STRIP)
        if not clause or clause in source:
            continue
        if sc.normalize(clause) in src_norm:
            continue
        if len(clause) > 1 and clause[0] in _PRONOUNS and (clause[1:] in source or sc.normalize(clause[1:]) in src_norm):
            continue
        bad.append(clause)
    return bad


MAX_FIX_DIFF = 2             # 逐字校正：與原文差幾個字以內就直接換成原文那一段
EXCERPT_CHARS = 50           # 錯誤訊息裡貼的原文片段長度上限


def _levenshtein(left, right):
    """兩段文字的編輯距離（Levenshtein，只用標準函式庫）。"""
    if len(left) < len(right):
        left, right = right, left
    previous = list(range(len(right) + 1))
    for row, char in enumerate(left, 1):
        current = [row]
        for column, other in enumerate(right, 1):
            current.append(min(previous[column] + 1, current[column - 1] + 1,
                               previous[column - 1] + (char != other)))
        previous = current
    return previous[-1]


def closest_source_fragments(clause, source):
    """原文裡與這個子句最接近的片段（忽略標點）→ (距離, 依原文順序的片段清單)。
    候選是原文裡長度差 ≤ 1 的每一段連續文字；距離最小的全部回傳，兩個以上不同片段就是有歧義。"""
    target = sc.normalize(clause)
    source = str(source or "")
    if not target or not source:
        return None, []
    norm, back = "", []
    for position, char in enumerate(source):
        piece = sc.normalize(char)
        if piece:
            norm += piece
            back += [position] * len(piece)
    found = {}
    for size in (len(target) - 1, len(target), len(target) + 1):
        if size < 1:
            continue
        for start in range(0, len(norm) - size + 1):
            distance = _levenshtein(target, norm[start:start + size])
            fragment = source[back[start]:back[start + size - 1] + 1]
            if fragment not in found.setdefault(distance, []):
                found[distance].append(fragment)
    if not found:
        return None, []
    best = min(found)
    return best, sorted(found[best], key=source.find)


def fix_verbatim(text, source, where):
    """逐字校正（微7-4）：與原文差 ≤ MAX_FIX_DIFF 字的子句直接換成原文那一段 → (新文字, warnings)。
    差更多、或有多個候選並列最佳（有歧義）就不動，留給 expand() 判錯——猜錯比判錯貴。"""
    fixed, warnings = str(text or ""), []
    for clause in unfaithful_clauses(fixed, source):
        distance, fragments = closest_source_fragments(clause, source)
        if distance is None or distance > MAX_FIX_DIFF or len(fragments) != 1:
            continue
        warnings.append("%s：逐字校正「%s」→「%s」（差 %d 字，已換成原文）" % (where, clause, fragments[0], distance))
        fixed = fixed.replace(clause, fragments[0], 1)
    return fixed, warnings


def source_excerpt(source, clause):
    """錯誤訊息要貼的原文片段：以最接近的子句為中心，超過 EXCERPT_CHARS 字用「…」截斷。"""
    source = str(source or "")
    if len(source) <= EXCERPT_CHARS:
        return source
    _distance, fragments = closest_source_fragments(clause, source)
    start = 0
    if fragments:
        at = source.find(fragments[0])
        if at >= 0:
            start = max(0, at - max(0, EXCERPT_CHARS - len(fragments[0])) // 2)
    start = min(start, len(source) - EXCERPT_CHARS)
    piece = source[start:start + EXCERPT_CHARS]
    return ("…" if start > 0 else "") + piece + ("…" if start + EXCERPT_CHARS < len(source) else "")


def omitted_clauses(source, line_texts):
    """原段落裡沒有出現在任何一行的子句（忽略標點）。用來抓「整句敘述被刪掉」。"""
    joined = sc.normalize("".join(str(t or "") for t in line_texts))
    missing = []
    for clause in _OMIT_SPLIT.split(str(source or "")):
        clause = clause.strip(_CLAUSE_STRIP)
        if len(sc.normalize(clause)) < 2 or _SPEECH_TAG.match(clause):
            continue
        if sc.normalize(clause) not in joined:
            missing.append(clause)
    return missing


def paragraph_omissions(lines, paragraphs):
    """每個有出現在 lines 的段落 → 被刪掉的子句。"""
    by_para = {}
    for line in lines or []:
        if isinstance(line, dict) and line.get("type") in ("narration", "dialogue") and isinstance(line.get("source_para"), int):
            by_para.setdefault(line["source_para"], []).append(str(line.get("text") or ""))
    out = {}
    for para, texts in sorted(by_para.items()):
        if para in (paragraphs or {}):
            missing = omitted_clauses(paragraphs[para], texts)
            if missing:
                out[para] = missing
    return out


def _missing_spans(source, line_texts):
    """原段落中被漏掉的敘述片段（原文偏移 start, end），相鄰的子句合併成一段。"""
    joined = sc.normalize("".join(str(t or "") for t in line_texts))
    spans = []
    pieces, cursor = [], 0
    for match in _OMIT_SPLIT.finditer(source):
        pieces.append((cursor, match.start(), match.end(), match.group()))
        cursor = match.end()
    pieces.append((cursor, len(source), len(source), ""))
    for start, end, _next, delim in pieces:
        clause = source[start:end].strip(_CLAUSE_STRIP)
        if len(sc.normalize(clause)) < 2 or _SPEECH_TAG.match(clause) or sc.normalize(clause) in joined:
            continue
        stop = end + (1 if delim and delim in "，。？！、；：,.?!;:．" else 0)
        if spans and source[spans[-1][1]:start].strip(_CLAUSE_STRIP + "，、") == "":
            spans[-1] = (spans[-1][0], stop)
        else:
            spans.append((start, stop))
    return spans


def repair_omissions(scene_doc, paragraphs):
    """把被漏掉的敘述原文補回 scene graph（照原文順序插進該段落的行之間）。回傳補了幾行。"""
    added = 0
    for scene in scene_doc.get("scenes") or []:
        lines = scene.get("lines") if isinstance(scene, dict) else None
        if not isinstance(lines, list):
            continue
        for para in sorted({line["source_para"] for line in lines
                            if isinstance(line, dict) and isinstance(line.get("source_para"), int)}):
            source = (paragraphs or {}).get(para)
            if source is None:
                continue
            idx = [i for i, line in enumerate(lines)
                   if isinstance(line, dict) and line.get("source_para") == para and line.get("type") in ("narration", "dialogue")]
            spans = _missing_spans(source, [lines[i].get("text") for i in idx])
            if not spans:
                continue
            # 每個現有行在原文中的位置（以去標點後的文字對位）
            norm_src, back = "", []
            for pos, char in enumerate(source):
                piece = sc.normalize(char)
                if piece:
                    norm_src += piece
                    back.append(pos)
            positions, cursor = [], 0
            for i in idx:
                piece = sc.normalize(lines[i].get("text"))
                at = norm_src.find(piece, cursor) if piece else -1
                if at < 0:
                    at = norm_src.find(piece) if piece else -1
                if at >= 0:
                    positions.append(back[at])
                    cursor = at + len(piece)
                else:
                    positions.append(positions[-1] if positions else 0)
            new_by_slot = {}
            for start, end in spans:
                text = source[start:end].strip()
                if text and text[-1] in "，、；：,;:":
                    text = text[:-1] + "。"       # 句尾不留逗號（標點不影響逐字比對）
                slot = next((k for k, pos in enumerate(positions) if pos > start), len(idx))
                for piece in split_text(text):
                    new_by_slot.setdefault(slot, []).append(
                        {"type": "narration", "text": piece, "source_para": para})
                    added += 1
            rebuilt, slot_at = [], {i: k for k, i in enumerate(idx)}
            for i, line in enumerate(lines):
                if i in slot_at:
                    rebuilt += new_by_slot.pop(slot_at[i], [])
                rebuilt.append(line)
                if i == idx[-1]:
                    rebuilt += new_by_slot.pop(len(idx), [])
            lines[:] = rebuilt
    return added


# ---------------------------------------------------------------------------
# 展開 op → scene graph，並收集所有問題
# ---------------------------------------------------------------------------
def _english_issues(value, label, where):
    if not isinstance(value, str) or not value.strip():
        return [Issue("error", where, "缺少 %s（要寫一句英文）" % label)]
    if CJK.search(value):
        return [Issue("error", where, "%s 要寫英文（生圖提示詞用英文），目前含有中文：「%s」" % (label, str(value)[:40]))]
    return []


def _ranges(numbers):
    out, start, prev = [], None, None
    for number in numbers:
        if start is None:
            start = prev = number
        elif number == prev + 1:
            prev = number
        else:
            out.append("%d" % start if start == prev else "%d–%d" % (start, prev))
            start = prev = number
    if start is not None:
        out.append("%d" % start if start == prev else "%d–%d" % (start, prev))
    return "、".join(out)


def _cast_entries(raw_cast, project, where, issues):
    """出場名單（只留 bible/characters 的角色；expression 要英文）。"""
    cast, seen = [], set()
    for index, entry in enumerate(raw_cast or [], 1):
        if not isinstance(entry, dict) or not entry.get("id"):
            issues.append(Issue("error", "%s cast[%d]" % (where, index), "每一項要是 {\"id\": 角色id, \"expression\": 英文表情}"))
            continue
        cid = entry["id"]
        if cid not in project.characters:
            issues.append(Issue("error", "%s cast[%d]" % (where, index),
                                "角色「%s」不在 bible/characters（現有：%s）" % (cid, "、".join(project.characters) or "無")))
            continue
        if cid in seen:
            issues.append(Issue("error", "%s cast[%d]" % (where, index), "角色「%s」在同一場出現兩次" % cid))
            continue
        seen.add(cid)
        item = {"id": cid}
        expression = entry.get("expression")
        if expression:
            problems = _english_issues(expression, "角色「%s」的 expression" % cid, where)
            issues += problems
            if not problems:
                item["expression"] = str(expression)
        cast.append(item)
    return cast


def expand(ops_doc, project, chapter, paragraphs, title=""):
    """把模型輸出的 op 展開成 scene graph。issues 是空清單才代表可以採用。"""
    issues, warnings = [], []
    para_map = paragraph_map(paragraphs)
    last = len(para_map)
    if not last:
        return Expanded({}, [Issue("error", "", "原著是空的，沒有段落可以切對話")], warnings)
    raw_scenes = ops_doc.get("scenes") if isinstance(ops_doc, dict) else None
    if not isinstance(raw_scenes, list) or not raw_scenes:
        return Expanded({}, [Issue("error", "", "最外層要是 {\"scenes\": [ … ]}，而且 scenes 不能是空的")], warnings)
    if len(raw_scenes) > MAX_SCENES:
        return Expanded({}, [Issue("error", "", "場數 %d 太多（上限 %d）" % (len(raw_scenes), MAX_SCENES))], warnings)

    previous, scenes, corrected = 0, [], []
    for index, raw in enumerate(raw_scenes, 1):
        where = "第 %d 場" % index
        if not isinstance(raw, dict):
            issues.append(Issue("error", where, "每一場要是物件 {title, location, setting_en, cast, ops}"))
            continue
        location = raw.get("location")
        if location in (None, ""):
            location = None
        elif not isinstance(location, str) or location not in project.locations:
            issues.append(Issue("error", where, "地點「%s」不在 bible/locations（現有：%s）；沒有合適的就省略 location"
                                % (location, "、".join(project.locations) or "無")))
            location = None
        setting = raw.get("setting_en")
        issues += _english_issues(setting, "setting_en", where)
        cast = _cast_entries(raw.get("cast"), project, where, issues)
        cast_seen = {item["id"] for item in cast}

        ops = raw.get("ops")
        if not isinstance(ops, list) or not ops:
            issues.append(Issue("error", where, "缺少 ops（這一場的旁白／對話／演出）"))
            ops = []
        lines, scene_paras = [], []
        for n, op in enumerate(ops, 1):
            w = "%s第 %d 個 op" % (where, n)
            if not isinstance(op, dict) or op.get("k") not in OP_KINDS:
                issues.append(Issue("error", w, "k 只能是 N、n、d、x 其中之一"))
                continue
            kind_op = op["k"]
            if kind_op == "x":                                  # 演出：漫畫的鏡頭、角度、一句英文的重點
                entry = {"type": "direction"}
                if op.get("shot") not in (None, ""):
                    if op["shot"] in SHOT:
                        entry["shot"] = op["shot"]
                    else:
                        issues.append(Issue("error", w, "shot「%s」不認得（可用：%s）" % (op["shot"], "、".join(SHOT))))
                if op.get("angle") not in (None, ""):
                    if op["angle"] in ANGLE:
                        entry["angle"] = op["angle"]
                    else:
                        issues.append(Issue("error", w, "angle「%s」不認得（可用：%s）" % (op["angle"], "、".join(ANGLE))))
                if op.get("note"):
                    problems = _english_issues(op["note"], "note", w)
                    issues += problems
                    if not problems:
                        entry["note"] = str(op["note"]).strip()
                if len(entry) == 1:
                    issues.append(Issue("error", w, "演出至少要寫 shot、angle、note 其中一個"))
                    continue
                lines.append(entry)
                continue
            para = op.get("p")
            if not isinstance(para, int) or isinstance(para, bool) or not 1 <= para <= last:
                issues.append(Issue("error", w, "p 要是 1–%d 的段落號，目前是 %r" % (last, para)))
                continue
            if para < previous:
                issues.append(Issue("error", w, "p %d 又回到前面的段落（上一個是 %d）；請照原著順序" % (para, previous)))
            previous = max(previous, para)
            source = para_map[para]
            if kind_op == "N":                                   # 整段旁白（原文照抄）
                if has_quoted_speech(source):
                    issues.append(Issue("error", w, "段落 %d 裡有引號台詞，N 會把台詞當旁白；請用 n 打敘述、d 打每一句台詞" % para))
                    continue
                lines += [{"type": "narration", "text": piece, "source_para": para} for piece in split_text(source)]
                scene_paras.append(para)
                continue
            text = op.get("t")
            if not isinstance(text, str) or not text.strip():
                issues.append(Issue("error", w, "缺少 t（要從段落 %d 逐字抄一段）" % para))
                continue
            text, corrections = fix_verbatim(text, source, w)     # 微7-4：先逐字校正，再判錯
            warnings += corrections
            corrected += corrections
            bad = unfaithful_clauses(text, source)
            if bad:
                other = locate_clause(bad[0], para_map, para)
                hint = "（這句在段落 %d，請把 p 改成 %d）" % (other, other) if other else ""
                issues.append(Issue("error", w, "這不是段落 %d 的原文：%s%s；原文是：%s"
                                    % (para, "／".join(bad[:3]), hint, source_excerpt(source, bad[0]))))
            if kind_op == "n":                                   # 段落裡的敘述片段
                lines += [{"type": "narration", "text": piece, "source_para": para} for piece in split_text(text)]
                scene_paras.append(para)
                continue
            speaker = op.get("s")                                # 對話
            if speaker not in project.characters:
                issues.append(Issue("error", w, "說話的角色「%s」不在 bible/characters（現有：%s）"
                                    % (speaker, "、".join(project.characters) or "無")))
                continue
            if speaker not in cast_seen:                         # 說話的人漏列在 cast：程式補上（同 t2vn）
                cast_seen.add(speaker)
                item = {"id": speaker}
                expression = op.get("e")
                if expression and not CJK.search(str(expression)):
                    item["expression"] = str(expression)
                cast.append(item)
                warnings.append("%s：角色「%s」有台詞但沒列在 cast，已自動補上" % (where, speaker))
            kind, offscreen = parse_marks(str(op.get("m") or ""), w, issues)
            line = {"type": "dialogue", "speaker": speaker, "kind": kind, "text": text.strip(), "source_para": para}
            if offscreen:
                line["offscreen"] = True
            lines.append(line)
            scene_paras.append(para)

        if not any(line.get("type") in ("narration", "dialogue") for line in lines):
            issues.append(Issue("error", where, "這一場只有演出，沒有旁白或對話"))
        if len(set(scene_paras)) > MAX_SCENE_PARAGRAPHS:
            issues.append(Issue("error", where, "這一場涵蓋 %d 個段落（上限 %d），請在地點、時間或焦點改變的地方拆成幾場"
                                % (len(set(scene_paras)), MAX_SCENE_PARAGRAPHS)))
        scene = {"scene_id": "ch%03d_sc%02d" % (int(chapter), index)}
        if isinstance(raw.get("title"), str) and raw["title"].strip():
            scene["title"] = raw["title"].strip()
        scene["location"] = location
        scene["setting_en"] = str(setting or "").strip()
        scene["cast"] = cast
        scene["lines"] = lines
        scenes.append(scene)

    source_path = project.source_path(chapter)
    doc = {"version": SCHEMA_VERSION, "chapter": int(chapter), "title": title or "",
           "source": {"file": source_path.name if source_path else "", "paragraphs": last},
           "scenes": scenes}
    if not issues:
        issues = validate(doc, project, paragraphs)
    issues += [Issue("warn", "", note) for note in corrected]     # 校正過哪幾格也要進報告（warn 不觸發回修）
    return Expanded(doc, issues, warnings)


def validate(scene_doc, project, paragraphs):
    """檢查一份展開後的 scene graph（模型剛產生的、人改過的、或從舊劇本反推的都走這裡）。"""
    issues = []
    para_map = paragraph_map(paragraphs)
    last = len(para_map)
    if not isinstance(scene_doc, dict):
        return [Issue("error", "", "scenes 檔案最外層要是物件")]
    scenes = scene_doc.get("scenes")
    if not isinstance(scenes, list) or not scenes:
        return [Issue("error", "", "沒有 scenes（至少要一場）")]
    if len(scenes) > MAX_SCENES:
        issues.append(Issue("error", "", "場數 %d 太多（上限 %d）" % (len(scenes), MAX_SCENES)))
    used, previous = set(), 0
    for index, scene in enumerate(scenes, 1):
        where = "第 %d 場" % index
        if not isinstance(scene, dict):
            issues.append(Issue("error", where, "每一場要是物件"))
            continue
        issues += _english_issues(scene.get("setting_en"), "setting_en", where)
        location = scene.get("location")
        if location not in (None, "") and (not isinstance(location, str) or location not in project.locations):
            issues.append(Issue("error", where, "地點「%s」不在 bible/locations（現有：%s）"
                                % (location, "、".join(project.locations) or "無")))
        cast = [item.get("id") for item in scene.get("cast") or [] if isinstance(item, dict)]
        for cid in cast:
            if cid not in project.characters:
                issues.append(Issue("error", where, "角色「%s」不在 bible/characters" % cid))
        lines = scene.get("lines")
        if not isinstance(lines, list) or not lines:
            issues.append(Issue("error", where, "lines 不能是空的"))
            continue
        if not any(isinstance(line, dict) and line.get("type") in ("narration", "dialogue") for line in lines):
            issues.append(Issue("error", where, "這一場只有演出，沒有旁白或對話"))
        scene_paras = set()
        for n, line in enumerate(lines, 1):
            w = "%s第 %d 行" % (where, n)
            if not isinstance(line, dict):
                issues.append(Issue("error", w, "每一行要是物件"))
                continue
            kind = line.get("type")
            if kind == "direction":
                if line.get("shot") not in (None, "") and line["shot"] not in SHOT:
                    issues.append(Issue("error", w, "shot「%s」不認得" % line["shot"]))
                if line.get("angle") not in (None, "") and line["angle"] not in ANGLE:
                    issues.append(Issue("error", w, "angle「%s」不認得" % line["angle"]))
                if not any(line.get(key) for key in ("shot", "angle", "note")):
                    issues.append(Issue("error", w, "演出至少要寫 shot、angle、note 其中一個"))
                if line.get("note"):
                    issues += _english_issues(line["note"], "note", w)
                continue
            if kind not in LINE_KINDS:
                issues.append(Issue("error", w, "type 只能是 narration、dialogue、direction"))
                continue
            text = str(line.get("text") or "").strip()
            if not text:
                issues.append(Issue("error", w, "缺少 text"))
                continue
            para = line.get("source_para")
            if not isinstance(para, int) or isinstance(para, bool) or para not in para_map:
                issues.append(Issue("error", w, "source_para 要是 1–%d 的段落號，目前是 %r" % (last, para)))
                continue
            if para < previous:
                issues.append(Issue("error", w, "source_para %d 又回到前面的段落（上一行是 %d）；台詞要照原著順序" % (para, previous)))
            previous = max(previous, para)
            used.add(para)
            scene_paras.add(para)
            bad = unfaithful_clauses(text, para_map[para])
            if bad:
                other = locate_clause(bad[0], para_map, para)
                hint = "（這句在段落 %d）" % other if other else ""
                issues.append(Issue("error", w, "不是段落 %d 的原文：%s%s；原文是：%s"
                                    % (para, "／".join(bad[:3]), hint, source_excerpt(para_map[para], bad[0]))))
            if kind == "narration":
                continue
            speaker = line.get("speaker")
            if speaker not in project.characters:
                issues.append(Issue("error", w, "說話的角色「%s」不在 bible/characters（現有：%s）"
                                    % (speaker, "、".join(project.characters) or "無")))
            elif speaker not in cast:
                issues.append(Issue("error", w, "「%s」說了話，但不在這一場的 cast；請把他加進 cast" % speaker))
            bubble_kind = line.get("kind", "speech")
            if bubble_kind not in BUBBLE_KINDS or bubble_kind == "narration":
                issues.append(Issue("error", w, "對話的 kind 只能是 %s" % "、".join(sorted(BUBBLE_KINDS - set(["narration"])))))
            offscreen = line.get("offscreen", False)
            if not isinstance(offscreen, bool):
                issues.append(Issue("error", w, "offscreen 只能是 true 或 false"))
            elif offscreen and bubble_kind == "thought":
                issues.append(Issue("error", w, "心聲要看得到想的人，不能標（畫面外）"))
        if len(scene_paras) > MAX_SCENE_PARAGRAPHS:
            issues.append(Issue("error", where, "這一場涵蓋 %d 個段落（上限 %d）" % (len(scene_paras), MAX_SCENE_PARAGRAPHS)))
        for para in sorted(paragraph_omissions([line for line in lines if isinstance(line, dict)], para_map)):
            clauses = paragraph_omissions([line for line in lines if isinstance(line, dict)], para_map)[para]
            issues.append(Issue("warn", "%s段落 %d" % (where, para),
                                "漏掉敘述「%s」%s；預設會由漏句修補照原文補回（也可以自己用 n op 補）"
                                % ("／".join(clauses[:3]), "…" if len(clauses) > 3 else "")))
    missing = sorted(set(para_map) - used)
    if missing:
        issues.append(Issue("error", "", "原著有 %d 段沒用到：%s。每一段都要出現在某一行的旁白或對話裡（整段沒有台詞就用 N）"
                            % (len(missing), _ranges(missing))))
    return issues


# ---------------------------------------------------------------------------
# 忠實度報告（「對話能在原著逐字找到」「原著引號台詞保留」與 script.fidelity 同一算法，可前後比較）
# ---------------------------------------------------------------------------
def fidelity(scene_doc, source, repaired=0):
    paragraphs = split_paragraphs(source)
    para_map = paragraph_map(paragraphs)
    lines = [line for scene in (scene_doc.get("scenes") or []) if isinstance(scene, dict)
             for line in (scene.get("lines") or []) if isinstance(line, dict)]
    text_lines = [line for line in lines if line.get("type") in ("narration", "dialogue")]
    dialogue = [line for line in text_lines if line.get("type") == "dialogue"]
    src_norm, sentences = sc.normalize(source), sc.source_sentences(source)
    found = {index for index, line in enumerate(dialogue)
             if sc.found_in(src_norm, str(line.get("text") or ""), sentences)}
    not_in_source = [str(dialogue[i].get("text") or "") for i in range(len(dialogue)) if i not in found]
    unfaithful = [clause for line in text_lines
                  for clause in unfaithful_clauses(str(line.get("text") or ""), para_map.get(line.get("source_para"), ""))]
    joined = sc.normalize("".join(str(line.get("text") or "") for line in text_lines))
    quotes = sc.source_quotes(source)
    quotes_missing = [quote for quote in quotes if sc.normalize(quote) not in joined]
    used = {line.get("source_para") for line in text_lines if isinstance(line.get("source_para"), int)}
    omissions = paragraph_omissions(text_lines, para_map)
    omitted = [clause for para in sorted(omissions) for clause in omissions[para]]
    return {
        "paragraphs": len(paragraphs),
        "paragraphs_used": len(used & set(para_map)),
        "scenes": len([scene for scene in (scene_doc.get("scenes") or []) if isinstance(scene, dict)]),
        "lines": len(text_lines),
        "dialogue_lines": len(dialogue),
        "dialogue_found": len(found),
        "not_in_source": not_in_source,
        "unfaithful": unfaithful,
        "source_quotes": len(quotes),
        "source_quotes_kept": len(quotes) - len(quotes_missing),
        "source_quotes_missing": quotes_missing,
        "omitted": omitted,
        "repaired": int(repaired or 0),
    }


def fidelity_markdown(report, title=""):
    total = report["paragraphs"] or 1
    lines = ["# 切對話忠實度報告%s" % ("：" + title if title else ""), "",
             "（程式比對原著；「對話能在原著逐字找到」「原著引號台詞保留」兩項與 build/adapt/chNNN_fidelity.md 同一算法，可以前後比較。）", "",
             "- 原著段落 %d 段，用到 %d 段（覆蓋 %d%%）" % (report["paragraphs"], report["paragraphs_used"],
                                                     round(100.0 * report["paragraphs_used"] / total)),
             "- 對話 %d 句，其中 %d 句能在原著逐字找到（不計標點）" % (report["dialogue_lines"], report["dialogue_found"]),
             "- 原著引號內台詞 %d 句，其中 %d 句進了切對話" % (report["source_quotes"], report["source_quotes_kept"]),
             "- 逐字違規 %d 句；漏掉的敘述子句 %d 句；漏句修補補了 %d 行" % (len(report["unfaithful"]), len(report["omitted"]), report["repaired"]), ""]
    if report["not_in_source"]:
        lines += ["## 切對話有、原著找不到的台詞（改寫或新增）", ""] + ["- %s" % text for text in report["not_in_source"]] + [""]
    if report["unfaithful"]:
        lines += ["## 逐字違規（不是該段原文的子句）", ""] + ["- %s" % text for text in report["unfaithful"]] + [""]
    if report["source_quotes_missing"]:
        lines += ["## 原著有、切對話沒有的台詞（刪除或改寫）", ""] + ["- %s" % text for text in report["source_quotes_missing"]] + [""]
    if report["omitted"]:
        lines += ["## 原著有、切對話沒有的敘述子句（漏句）", ""] + ["- %s" % text for text in report["omitted"]] + [""]
    return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------------------
# 轉分鏡：scene graph → storyboard（compile_chapter 吃的格式）
# ---------------------------------------------------------------------------
def _columns(text, width):
    """台詞依直排欄寬斷行（嵌字每一欄最多 12 字、旁白 16 字）。標點不要掉到下一欄開頭。"""
    text = re.sub(r"\s+", "", str(text or ""))
    limit = width + 1                       # 一欄最多 width＋1 個字（標點可以跟著擠進前一欄）
    columns, index = [], 0
    while index < len(text):
        size = min(width, len(text) - index)
        run = _tail_run(text, index + size)
        while size > 1 and size + run > limit:      # 標點擠不下就整欄往前挪一個字
            size -= 1
            run = _tail_run(text, index + size)
        columns.append(text[index:index + size + run])
        index += size + run
    return "\n".join(columns)


def _tail_run(text, start):
    """從 start 開始連續幾個「不能放在欄首」的標點。"""
    run = 0
    while start + run < len(text) and text[start + run] in _COLUMN_TAIL:
        run += 1
    return run


def _template_for_count(count, pages_data):
    templates = pages_data["templates"]
    for key in ("1", "2h", "3a", "4a", "5a", "6a"):
        if key in templates and len(templates[key]["panels"]) == count:
            return key
    ordered = sorted(templates, key=lambda key: len(templates[key]["panels"]))
    fit = [key for key in ordered if len(templates[key]["panels"]) >= count]
    return fit[0] if fit else ordered[-1]


def _auto_pages(total, pages_data):
    """沒有改編規劃時，依行數自己配頁：一頁最多 6 格、一格最多 3 個對話框。"""
    keys, left = [], total
    while left > 0:
        count = max(1, min(6, -(-left // MAX_BUBBLES)))
        key = _template_for_count(count, pages_data)
        keys.append(key)
        left -= len(pages_data["templates"][key]["panels"]) * MAX_BUBBLES
    return keys


def _panel(slot, beats):
    scene = beats[0]["scene"] if beats else None
    setting = str((scene or {}).get("setting_en") or "").strip()
    notes = [beat["note"] for beat in beats if beat.get("note")]
    text = " ".join([setting] + notes).strip() or "A still frame with no dialogue."
    shot = next((beat["shot"] for beat in beats if beat.get("shot")), "medium")
    angle = next((beat["angle"] for beat in beats if beat.get("angle")), "eye")
    bubbles, speakers = [], []
    for beat in beats:
        kind = "narration" if beat["type"] == "narration" else beat.get("kind", "speech")
        bubble = {"kind": kind,
                  "text": _columns(beat["text"], NARRATION_CHARS if kind == "narration" else DIALOGUE_CHARS)}
        if kind != "narration":
            bubble["speaker"] = beat.get("speaker")
            # 透過通訊器、電話、廣播的聲音一律不畫說話的人（人要畫在畫面上就別標通訊，改用一般台詞）
            if beat.get("offscreen") or kind == "radio":
                bubble["offscreen"] = True
            elif beat.get("speaker") and beat["speaker"] not in speakers:
                speakers.append(beat["speaker"])
        bubble["anchor"] = list(_ANCHORS[len(bubbles) % len(_ANCHORS)])
        bubbles.append(bubble)
    if not bubbles or all(bubble["kind"] == "narration" for bubble in bubbles):
        speakers = [item.get("id") for item in (scene or {}).get("cast") or [] if isinstance(item, dict)][:2]
    expressions = {item.get("id"): item.get("expression")
                   for item in (scene or {}).get("cast") or [] if isinstance(item, dict)}
    characters = []
    boxes = _BOXES[len(speakers) - 1] if 0 < len(speakers) <= 3 else _BOXES[0]
    for index, cid in enumerate(speakers[:3]):
        entry = {"id": cid}
        if expressions.get(cid):
            entry["expression"] = expressions[cid]
        entry["box"] = boxes[index]
        characters.append(entry)
    drawn = {entry["id"] for entry in characters}          # 微7-3：人就在畫面上，就不是畫面外的聲音
    for bubble in bubbles:
        if bubble.get("offscreen") and bubble.get("speaker") in drawn:
            del bubble["offscreen"]
    panel = {"slot": slot, "shot": shot, "angle": angle}
    if (scene or {}).get("location"):
        panel["location"] = scene["location"]
    panel["scene"] = text
    panel["characters"] = characters
    panel["bubbles"] = bubbles
    return panel


def _page_of(pages_data, key, beats, position, page_no):
    count = len(pages_data["templates"][key]["panels"])
    panels, panels_left = [], count
    for slot in range(1, count + 1):
        left = len(beats) - position
        take = 0 if left <= 0 else max(1, min(MAX_BUBBLES, -(-left // panels_left)))
        panels.append(_panel(slot, beats[position:position + take]))
        position += take
        panels_left -= 1
    return {"page": page_no, "template": key, "panels": panels}, position


def to_storyboard(project, scene_doc, pages_data=None, plan=None, compile_check=True):
    """scene graph → storyboard（panels／bubbles）。台詞逐字複製，程式只負責斷行、anchor 與角色框。

    compile_check=True 時會真的呼叫 compile_chapter()；通過了才回傳，轉出來的東西保證能編譯。
    """
    pages_data = pages_data or project.pages()
    beats = []
    for scene in [item for item in (scene_doc.get("scenes") or []) if isinstance(item, dict)]:
        pending = {}
        for line in scene.get("lines") or []:
            if not isinstance(line, dict):
                continue
            if line.get("type") == "direction":
                pending = {key: line[key] for key in ("shot", "angle", "note") if line.get(key)}
                continue
            if line.get("type") not in ("narration", "dialogue"):
                continue
            beat = {"type": line["type"], "text": str(line.get("text") or ""), "scene": scene,
                    "shot": pending.get("shot"), "angle": pending.get("angle"), "note": pending.get("note")}
            if line["type"] == "dialogue":
                beat["speaker"] = line.get("speaker")
                beat["kind"] = line.get("kind", "speech")
                beat["offscreen"] = bool(line.get("offscreen"))
            beats.append(beat)
    if not beats:
        raise AnnotateError("切對話裡沒有任何旁白或對話，無法轉成分鏡")

    keys = []
    if plan and plan.get("pages"):
        for page in plan["pages"]:
            key = str(page.get("template") or "")
            if key in pages_data["templates"]:
                keys.append(key)
        capacity = sum(len(pages_data["templates"][key]["panels"]) * MAX_BUBBLES for key in keys)
        if len(beats) > capacity:
            keys += _auto_pages(len(beats) - capacity, pages_data)
    if not keys:
        keys = _auto_pages(len(beats), pages_data)

    pages, position = [], 0
    for key in keys:
        if position >= len(beats):
            break
        page, position = _page_of(pages_data, key, beats, position, len(pages) + 1)
        pages.append(page)
    storyboard = {"version": 1, "chapter": int(scene_doc.get("chapter") or 0),
                  "title": str(scene_doc.get("title") or ""), "pages": pages}
    if compile_check:
        try:
            compile_chapter(project, storyboard, pages_data)
        except CompileError as error:
            raise AnnotateError("切對話轉成分鏡後無法編譯：%s" % error)
    return storyboard
