"""
workflow_adapter.py
-------------------
Thin mapping layer between the live conversation values the backend produces
each turn and the *specific node IDs* inside the RuneXX LTX-2.3 talking-avatar
ComfyUI workflow.

WHY THIS EXISTS (the FluxRT "verify signatures against real source" lesson):
ComfyUI's HTTP/WS API does NOT consume the editor-format .json you export from
the UI canvas (the one full of "nodes"/"links"/"groups"). It consumes the
*API/prompt format*: a flat dict {node_id: {"class_type", "inputs"}}.

So there are two formats and you must not confuse them:
  - EDITOR format  -> what you drag around in the browser, what you uploaded
  - API format     -> what /prompt and the websocket API actually run

This adapter operates on the API format. Convert once (see README:
"Export the API-format workflow") and save it as workflow_api.json next to this
file. Then this module patches only the handful of nodes that change per turn.

If RuneXX updates the graph and node numbers shift, you change the IDs HERE and
nowhere else. Everything upstream/downstream of these nodes (model loaders,
samplers, VAEs) ComfyUI caches between runs, so they are untouched.

------------------------------------------------------------------------------
NODE IDs traced from the uploaded editor graph (cross-checked against links):

  444  LoadImage                  -> character reference image (filename in
                                      ComfyUI's input/ dir)
  1941 LoadAudio                  -> voice-clone REFERENCE audio (the character's
                                      voice sample; static per character)
  1944 AILab_Qwen3TTSVoiceClone   -> .target_text  = what the avatar SAYS
  1942 PrimitiveStringMultiline   -> "Prompt (what to say)" feeds Qwen target_text
                                      path in T2V; also the scene/dialogue prompt
  1624 PrimitiveStringMultiline   -> "PROMPT" : the LTX-2 scene/dialogue prompt
  1938 PrimitiveStringMultiline   -> reference transcribe (optional)
  1583 INTConstant                -> LENGTH (in seconds)  [latency lever]
  1591 INTConstant                -> HEIGHT                [latency lever]
  1606 INTConstant                -> WIDTH                 [latency lever]
  1837 VHS_VideoCombine           -> final mp4 (we read its output)

IMPORTANT: when you export the API format, ComfyUI RE-NUMBERS some nodes and
flattens subgraphs (the Prompt-Enhancer and Calculate-Frames subgraphs in this
graph WILL expand). After exporting, run `python workflow_adapter.py --probe
workflow_api.json` to print every node whose class_type matches a target below,
then fix the IDs in NODE_IDS if needed. Do this ONCE; it takes two minutes and
saves the silent-wrong-node failure mode.
------------------------------------------------------------------------------
"""

from __future__ import annotations
import json
import copy
import argparse
from dataclasses import dataclass, field


# ----- Node IDs in the API-format workflow ------------------------------------
# These are the EDITOR ids. Re-verify against the API export with --probe.
NODE_IDS = {
    "load_image":       "444",
    "load_voice_ref":   "1941",
    "qwen_tts":         "1944",
    "prompt_say":       "1942",
    "prompt_scene":     "1624",
    "ref_transcribe":   "1938",
    "length_seconds":   "1583",
    "height":           "1591",
    "width":            "1606",
    "video_combine":    "1837",
}

# class_type each id is EXPECTED to have, used by --probe to catch renumbering.
EXPECTED_CLASS = {
    "load_image":     "LoadImage",
    "load_voice_ref": "LoadAudio",
    "qwen_tts":       "AILab_Qwen3TTSVoiceClone",
    "prompt_say":     "PrimitiveStringMultiline",
    "prompt_scene":   "PrimitiveStringMultiline",
    "ref_transcribe": "PrimitiveStringMultiline",
    "length_seconds": "INTConstant",
    "height":         "INTConstant",
    "width":          "INTConstant",
    "video_combine":  "VHS_VideoCombine",
}


@dataclass
class Turn:
    """Everything that varies per conversational turn."""
    say_text: str                       # what the avatar speaks (LLM reply)
    scene_prompt: str | None = None     # LTX scene/dialogue prompt; if None, built
    character_image: str | None = None  # filename already present in ComfyUI input/
    voice_ref_audio: str | None = None  # filename already present in ComfyUI input/
    length_seconds: int | None = None   # override clip length
    width: int | None = None
    height: int | None = None
    seed: int | None = None             # optional: vary the noise per turn
    fast: bool = False                  # skip the upscaler + 2nd-pass sampler


def build_scene_prompt(say_text: str, character_hint: str = "") -> str:
    """
    LTX-2 wants the spoken line embedded in an action description with an
    explicit lip-sync instruction (per the graph's own prompting notes and the
    RuneXX talking-avatar pattern). Keep it short: shorter prompt + shorter line
    = shorter clip = faster render.
    """
    who = character_hint.strip() or "The person"
    line = say_text.strip().rstrip()
    return (
        f"Style: realistic - cinematic - {who} looks at the camera and speaks "
        f"in a natural, expressive voice, saying: \"{line}\" "
        f"with perfect lip-sync to the audio. Subtle natural head movement. "
        f"Static camera."
    )


def _set_widget(node: dict, key: str, value):
    node.setdefault("inputs", {})[key] = value


# ----- fast mode: drop the upscaler + second-pass sampler ---------------------
# In API format a link is expressed on the CONSUMER side as
#   node["inputs"][slot_name] = [producer_id, producer_output_index]
# ComfyUI only executes nodes that an output transitively depends on, so if we
# re-point the final decoders at the FIRST-pass latent, the whole second-pass
# branch (spatial upsampler -> concat -> 2nd sampler -> separate) is never run.
#
# We locate nodes by class_type + topology rather than by hardcoded IDs, because
# IDs renumber when you export API format. The graph has TWO SamplerCustomAdvanced
# nodes and TWO LTXVSeparateAVLatent nodes; we tell them apart by depth:
#   first-pass sampler   = the SamplerCustomAdvanced whose latent_image traces
#                          back WITHOUT passing through an LTXVLatentUpsampler
#   second-pass sampler  = the one fed (via concat) from the upsampler
# The first-pass SeparateAVLatent is the one reading the first-pass sampler.

def _producers(node: dict) -> list[str]:
    out = []
    for v in node.get("inputs", {}).values():
        if isinstance(v, list) and len(v) == 2 and isinstance(v[0], (str, int)):
            out.append(str(v[0]))
    return out


def _find_by_class(wf: dict, cls: str) -> list[str]:
    return [nid for nid, n in wf.items() if n.get("class_type") == cls]


def _depends_on_class(wf: dict, start: str, target_cls: str,
                      _seen=None) -> bool:
    """True if `start` transitively consumes any node of class target_cls."""
    if _seen is None:
        _seen = set()
    if start in _seen:
        return False
    _seen.add(start)
    n = wf.get(start, {})
    if n.get("class_type") == target_cls:
        return True
    return any(_depends_on_class(wf, p, target_cls, _seen) for p in _producers(n))


def apply_fast_mode(wf: dict) -> list[str]:
    """
    Rewire so decode reads the first-pass latent; orphan the upscale branch.
    Returns a list of human-readable notes about what was changed (for logging).
    Raises RuntimeError if the expected topology isn't found, so we fail loud
    rather than silently shipping a broken graph.
    """
    notes = []
    samplers = _find_by_class(wf, "SamplerCustomAdvanced")
    separators = _find_by_class(wf, "LTXVSeparateAVLatent")
    upsamplers = _find_by_class(wf, "LTXVLatentUpsampler")

    if not upsamplers:
        # nothing to skip (graph may already be single-pass)
        return ["fast: no LTXVLatentUpsampler present; nothing to skip"]
    if len(samplers) < 2 or len(separators) < 2:
        raise RuntimeError(
            f"fast: expected 2 samplers + 2 separators, found "
            f"{len(samplers)} / {len(separators)}. Re-probe the graph."
        )

    # second-pass sampler = depends on the upsampler; first-pass = the other one
    second = [s for s in samplers if _depends_on_class(wf, s, "LTXVLatentUpsampler")]
    first = [s for s in samplers if s not in second]
    if len(first) != 1 or len(second) < 1:
        raise RuntimeError(
            f"fast: couldn't disambiguate first/second sampler "
            f"(first={first}, second={second})."
        )
    first_sampler = first[0]

    # first-pass separator = the LTXVSeparateAVLatent reading the first sampler
    first_sep = None
    for sep in separators:
        if first_sampler in _producers(wf[sep]):
            first_sep = sep
            break
    if first_sep is None:
        raise RuntimeError("fast: no separator reads the first-pass sampler.")

    # which output slot of the separator is video vs audio?
    # In this node, slot 0 = video_latent, slot 1 = audio_latent (per graph).
    video_src = [first_sep, 0]
    audio_src = [first_sep, 1]

    # Re-point the final decoders.
    rewired = 0
    for nid, n in wf.items():
        ct = n.get("class_type")
        if ct == "VAEDecodeTiled" or ct == "VAEDecode":
            if "samples" in n.get("inputs", {}):
                n["inputs"]["samples"] = list(video_src)
                rewired += 1
                notes.append(f"fast: {ct} {nid}.samples -> first-pass video")
        elif ct == "LTXVAudioVAEDecode":
            if "samples" in n.get("inputs", {}):
                n["inputs"]["samples"] = list(audio_src)
                rewired += 1
                notes.append(f"fast: {ct} {nid}.samples -> first-pass audio")

    if rewired == 0:
        raise RuntimeError("fast: found no decode nodes to rewire.")
    notes.append(f"fast: rewired {rewired} decode input(s); "
                 f"upscale branch now orphaned and will not execute")
    return notes


def patch(workflow_api: dict, turn: Turn, character_hint: str = "") -> dict:
    """
    Return a deep-copied API workflow with this turn's values injected.
    Never mutates the template passed in.
    """
    wf = copy.deepcopy(workflow_api)

    def node(name: str) -> dict:
        nid = NODE_IDS[name]
        if nid not in wf:
            raise KeyError(
                f"Node id {nid} ({name}) not in workflow. Run --probe to "
                f"re-map IDs after exporting API format."
            )
        return wf[nid]

    scene = turn.scene_prompt or build_scene_prompt(turn.say_text, character_hint)

    # 1) What the avatar SAYS -> Qwen TTS target_text
    _set_widget(node("qwen_tts"), "target_text", turn.say_text)

    # 2) Scene/dialogue prompt for LTX (both prompt boxes, so it works whether
    #    the active path reads the T2V box or the main PROMPT box).
    _set_widget(node("prompt_scene"), "value", scene)
    try:
        _set_widget(node("prompt_say"), "value", scene)
    except KeyError:
        pass  # prompt_say only present in some exports

    # 3) Character reference image (filename must already be uploaded to input/)
    if turn.character_image:
        _set_widget(node("load_image"), "image", turn.character_image)

    # 4) Voice-clone reference audio (static per character; only set if changing)
    if turn.voice_ref_audio:
        _set_widget(node("load_voice_ref"), "audio", turn.voice_ref_audio)

    # 5) Latency levers
    if turn.length_seconds is not None:
        _set_widget(node("length_seconds"), "value", int(turn.length_seconds))
    if turn.width is not None:
        _set_widget(node("width"), "value", int(turn.width))
    if turn.height is not None:
        _set_widget(node("height"), "value", int(turn.height))

    # 6) Seed: vary noise so repeated lines don't look identical. The graph has
    #    fixed-seed RandomNoise nodes; we nudge any we can find by class_type.
    if turn.seed is not None:
        for nid, n in wf.items():
            if n.get("class_type") == "RandomNoise":
                _set_widget(n, "noise_seed", int(turn.seed))

    # 7) Fast mode: skip upscaler + second-pass sampler (lower res, faster).
    if turn.fast:
        for line in apply_fast_mode(wf):
            print("[adapter]", line)

    return wf


def output_node_id() -> str:
    """ID of the VHS_VideoCombine node whose mp4 we harvest."""
    return NODE_IDS["video_combine"]


# ----- Dev helper: verify IDs against an API export ---------------------------
def _probe(path: str):
    with open(path, "r", encoding="utf-8") as f:
        wf = json.load(f)
    # Accept either a raw API dict or an editor export with an embedded prompt.
    if "extra" in wf and isinstance(wf.get("extra"), dict) and "prompt" in wf["extra"]:
        print("NOTE: this looks like an EDITOR export. The API workflow is under "
              "extra.prompt but that embedded copy is a DIFFERENT (older) graph. "
              "Export the API format properly via the UI instead. Probing it "
              "anyway for reference.\n")
        wf = wf["extra"]["prompt"]
    by_class: dict[str, list[str]] = {}
    for nid, n in wf.items():
        ct = n.get("class_type", "?")
        by_class.setdefault(ct, []).append(nid)

    print("Resolved target nodes:")
    ok = True
    for name, nid in NODE_IDS.items():
        want = EXPECTED_CLASS[name]
        got = wf.get(nid, {}).get("class_type")
        flag = "ok " if got == want else "!! "
        if got != want:
            ok = False
        print(f"  {flag}{name:16s} id={nid:6s} want={want:28s} got={got}")
    if not ok:
        print("\nMismatches found. For each, look below for the right id:")
        for name, nid in NODE_IDS.items():
            if wf.get(nid, {}).get("class_type") != EXPECTED_CLASS[name]:
                cands = by_class.get(EXPECTED_CLASS[name], [])
                print(f"  {name}: candidates with class "
                      f"{EXPECTED_CLASS[name]}: {cands}")
    else:
        print("\nAll target node IDs resolve correctly.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", metavar="WORKFLOW_API_JSON",
                    help="verify NODE_IDS against an exported workflow")
    args = ap.parse_args()
    if args.probe:
        _probe(args.probe)
    else:
        ap.print_help()
