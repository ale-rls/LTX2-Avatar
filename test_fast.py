import workflow_adapter as wfa

# Minimal API-format graph mirroring the real two-pass topology:
# first sampler -> sep1 -> (video) upsampler -> concat -> second sampler -> sep2 -> decodes
wf = {
  "noise1": {"class_type":"RandomNoise","inputs":{"noise_seed":1}},
  "first":  {"class_type":"SamplerCustomAdvanced","inputs":{"noise":["noise1",0],"latent_image":["emptylatent",0]}},
  "emptylatent":{"class_type":"EmptyLTXVLatentVideo","inputs":{}},
  "sep1":   {"class_type":"LTXVSeparateAVLatent","inputs":{"av_latent":["first",0]}},
  "ups":    {"class_type":"LTXVLatentUpsampler","inputs":{"samples":["sep1",0]}},
  "concat": {"class_type":"LTXVConcatAVLatent","inputs":{"video_latent":["ups",0],"audio_latent":["sep1",1]}},
  "second": {"class_type":"SamplerCustomAdvanced","inputs":{"noise":["noise2",0],"latent_image":["concat",0]}},
  "noise2": {"class_type":"RandomNoise","inputs":{"noise_seed":2}},
  "sep2":   {"class_type":"LTXVSeparateAVLatent","inputs":{"av_latent":["second",0]}},
  "vdec":   {"class_type":"VAEDecodeTiled","inputs":{"samples":["sep2",0],"vae":["vae",0]}},
  "adec":   {"class_type":"LTXVAudioVAEDecode","inputs":{"samples":["sep2",1],"audio_vae":["avae",0]}},
  "vae":{"class_type":"VAELoader","inputs":{}},
  "avae":{"class_type":"VAELoaderKJ","inputs":{}},
  "combine":{"class_type":"VHS_VideoCombine","inputs":{"images":["vdec",0],"audio":["adec",0]}},
}

notes = wfa.apply_fast_mode(wf)
print("\n".join(notes))
print()

# Assertions:
# 1) decode now reads from sep1 (first-pass), not sep2
assert wf["vdec"]["inputs"]["samples"] == ["sep1",0], wf["vdec"]["inputs"]["samples"]
assert wf["adec"]["inputs"]["samples"] == ["sep1",1], wf["adec"]["inputs"]["samples"]
print("OK decode rewired to first-pass separator")

# 2) verify the upscale branch is now orphaned (nothing the output depends on
#    reaches the upsampler)
def reaches(wf, start, target, seen=None):
    seen = seen or set()
    if start in seen: return False
    seen.add(start)
    n = wf.get(start,{})
    if start == target: return True
    prods=[]
    for v in n.get("inputs",{}).values():
        if isinstance(v,list) and len(v)==2: prods.append(str(v[0]))
    return any(reaches(wf,p,target,seen) for p in prods)

assert not reaches(wf,"combine","ups"), "upsampler still reachable from output!"
assert not reaches(wf,"combine","second"), "second sampler still reachable!"
assert not reaches(wf,"combine","sep2"), "sep2 still reachable!"
print("OK upscaler, second sampler, sep2 all orphaned from output")

# 3) first-pass path still intact
assert reaches(wf,"combine","first"), "first sampler not reachable!"
assert reaches(wf,"combine","sep1"), "sep1 not reachable!"
print("OK first-pass path intact to output")
print("\nALL FAST-MODE TESTS PASSED")
