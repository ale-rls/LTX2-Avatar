# ws_callbacks  -- DAT callbacks for the WebSocket DAT named 'ws'
# ----------------------------------------------------------------
# Assign this DAT as the "Callbacks DAT" of your WebSocket DAT.
# Each hook just forwards to the extension on the parent Base COMP.
# Nothing happens in TD unless this forwarding exists (FluxRT lesson:
# callbacks must explicitly call the extension).
#
# parent() here = the WebSocket DAT's owner = the Base COMP holding AvatarExt.

def onConnect(dat):
    parent().ext.AvatarExt.OnConnect()
    return

def onDisconnect(dat):
    parent().ext.AvatarExt.OnDisconnect()
    return

def onReceiveText(dat, rowIndex, message):
    parent().ext.AvatarExt.OnReceiveText(message)
    return

def onReceiveBinary(dat, contents):
    parent().ext.AvatarExt.OnReceiveBinary(contents)
    return

def onReceivePing(dat, contents):
    dat.sendPong(contents)
    return

def onReceivePong(dat, contents):
    return

def onMonitorMessage(dat, message):
    return
