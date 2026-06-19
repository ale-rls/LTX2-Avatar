# param_exec_callbacks.py  -- Parameter Execute DAT for the Avatar Base COMP
# ---------------------------------------------------------------------------
# Paste into a Text DAT and assign it as the callbacks DAT on the
# Parameter Execute DAT (named 'param_exec'). Enable:
#   - Custom Parameters
#   - Value Change
#   - Pulse
#
# Mirrors the DaydreamParexec pattern exactly:
#   onValueChange  -> ext.OnParameterChange(par)   for all value changes
#   onPulse        -> direct method per button name

def onValueChange(par, prev):
    ext = parent().ext.AvatarExt
    if ext:
        ext.OnParameterChange(par)
    return

def onValuesChanged(changes):
    for c in changes:
        par = c.par
        prev = c.prev
    return

def onPulse(par):
    ext = parent().ext.AvatarExt
    if not ext:
        return
    if par.name == 'Connect':
        ext.Connect()
    elif par.name == 'Reset':
        ext.Reset()
    elif par.name == 'Endturn':
        ext.EndTurn()
    return

def onExpressionChange(par, val, prev):
    return

def onExportChange(par, val, prev):
    return

def onEnableChange(par, val, prev):
    return

def onModeChange(par, val, prev):
    return
