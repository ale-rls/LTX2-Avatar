# ws_callbacks.py  — Web Server DAT callbacks for the LTX-2 Avatar relay
# -----------------------------------------------------------------------
# Paste into a Text DAT, assign as Callbacks DAT on the 'web_server' DAT.
# Mirrors the DaydreamWebServerCallbacks pattern with defensive getattr
# lookups (extension may not be loaded yet during COMP init).

def _ext(webServerDAT):
    return getattr(webServerDAT.parent().ext, 'AvatarExt', None)

def onHTTPRequest(webServerDAT, request, response):
    ext = _ext(webServerDAT)
    if ext:
        ext.OnHTTPRequest(request, response)
    else:
        response['statusCode'] = 503
        response['data'] = b'Extension not ready'
    return response

def onWebSocketOpen(webServerDAT, client, uri):
    ext = _ext(webServerDAT)
    if ext:
        ext.OnWebSocketOpen(client, uri)

def onWebSocketClose(webServerDAT, client):
    ext = _ext(webServerDAT)
    if ext:
        ext.OnWebSocketClose(client)

def onWebSocketReceiveText(webServerDAT, client, message):
    ext = _ext(webServerDAT)
    if ext:
        ext.OnWebSocketReceiveText(client, message)

def onWebSocketReceiveBinary(webServerDAT, client, message):
    ext = _ext(webServerDAT)
    if ext:
        ext.OnWebSocketReceiveBinary(client, message)

def onWebSocketReceivePing(webServerDAT, client, data):
    return

def onWebSocketReceivePong(webServerDAT, client, data):
    return

def onServerStart(webServerDAT):
    print(f"AvatarExt: Web Server started on port {webServerDAT.par.port.eval()}")

def onServerStop(webServerDAT):
    print("AvatarExt: Web Server stopped")
