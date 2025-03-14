#! /usr/bin/env python3
# -*- coding: utf-8 -*-
import asyncio
import json
import logging.config
import os
import shutil
import multiprocessing
import signal
import time

import uvicorn
import yaml
from fastapi import Body, Request, FastAPI, HTTPException, Security, Depends
from fastapi.security import APIKeyHeader
from sse_starlette.sse import EventSourceResponse
from starlette.status import HTTP_401_UNAUTHORIZED

from wcferry import Wcf, WxMsg

LOG = logging.getLogger("WCF-HTTP")

pwd = os.path.dirname(os.path.abspath(__file__))
try:
    with open(f"{pwd}/config.yaml", "rb") as fp:
        yconfig = yaml.safe_load(fp)
except FileNotFoundError:
    shutil.copyfile(f"{pwd}/config.yaml.template", f"{pwd}/config.yaml")
    with open(f"{pwd}/config.yaml", "rb") as fp:
        yconfig = yaml.safe_load(fp)

API_TOKENS = yconfig["api_tokens"]
SEND_RATE_LIMIT = yconfig.get("send_rate_limit", 0)

wcf = Wcf(debug=True)


def handler(sig, frame):
    wcf.cleanup()
    exit(0)


signal.signal(signal.SIGINT, handler)

subscribers = set()


def pubsub(wcf: Wcf):
    wcf.enable_receiving_msg(pyq=True)
    while wcf.is_receiving_msg():
        if not wcf.is_receiving_msg():
            LOG.error("WCF is not receiving messages")
            time.sleep(1)
            continue
        try:
            msg = wcf.get_msg()
            dead_subscribers = set()
            for subscriber in subscribers:
                try:
                    subscriber(msg)
                except Exception as e:
                    dead_subscribers.add(subscriber)
                    LOG.error(f"Error in subscriber: {e}")
        except Exception as e:
            LOG.error(f"Receiving message error: {e}")


app = FastAPI(title="WCF HTTP")


# Start the pubsub process when the application starts
@app.on_event("startup")
async def startup_event():
    process = multiprocessing.Process(target=pubsub, args=(wcf,))
    process.daemon = True  # Make the process daemon so it exits when the main program exits
    process.start()
    wcf.send_text("WCF HTTP 服务已启动", "filehelper")


async def verify_token(api_key: str = Security(APIKeyHeader(name="X-API-Token", auto_error=False))):
    if api_key not in API_TOKENS:
        raise HTTPException(
            status_code=HTTP_401_UNAUTHORIZED,
            detail="Invalid API Token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return True


@app.get("/")
def read_root(authenticated: bool = Depends(verify_token)):
    return {"message": "Service is running"}


@app.get("/subscribe")
def subscribe(request: Request, authenticated: bool = Depends(verify_token)):
    msg_queue = asyncio.Queue()

    async def subscriber(msg: WxMsg):
        await msg_queue.put({
            "id": msg.id,
            "ts": msg.ts,
            "sign": msg.sign,
            "type": msg.type,
            "xml": msg.xml,
            "sender": msg.sender,
            "roomid": msg.roomid,
            "content": msg.content,
            "thumb": msg.thumb,
            "extra": msg.extra,
            "is_at": msg.is_at(wcf.self_wxid),
            "is_self": msg.from_self(),
            "is_group": msg.from_group(),
        })

    subscribers.add(subscriber)

    async def event_generator():
        try:
            while True:
                if await request.is_disconnected():
                    break
                msg = await msg_queue.get()
                yield {
                    "event": "message",
                    "data": json.dumps(msg)
                }
        except Exception as e:
            print(f"Client disconnected: {e}")
        finally:
            subscribers.discard(subscriber)

    return EventSourceResponse(event_generator())


@app.post("/send-text")
def send_text(
        msg: str = Body(description="要发送的消息，换行用\\n表示"),
        receiver: str = Body("filehelper", description="消息接收者，roomid 或者 wxid"),
        aters: str = Body("", description="要 @ 的 wxid，多个用逗号分隔；@所有人 用 notify@all"),
        authenticated: bool = Depends(verify_token),
):
    try:
        ret = wcf.send_text(msg, receiver, aters)
        return {"status": "ok", "data": ret}
    except Exception as e:
        return {"status": "error", "message": str(e)}

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
