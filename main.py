#! /usr/bin/env python3
# -*- coding: utf-8 -*-
import asyncio
import json
import os
import shutil
import threading
import time
import traceback
import base64
import tempfile
import re
from queue import Empty

import uvicorn
import yaml
from fastapi import Body, Request, FastAPI, HTTPException, Security, Depends, UploadFile, File
from fastapi.security import APIKeyHeader
from sse_starlette.sse import EventSourceResponse
from starlette.status import HTTP_401_UNAUTHORIZED
from contextlib import asynccontextmanager

from wcferry import Wcf, WxMsg

pwd = os.path.dirname(os.path.abspath(__file__))
try:
    with open(f"{pwd}/config.yaml", "rb") as fp:
        yconfig = yaml.safe_load(fp)
except FileNotFoundError:
    shutil.copyfile(f"{pwd}/config.yaml.template", f"{pwd}/config.yaml")
    with open(f"{pwd}/config.yaml", "rb") as fp:
        yconfig = yaml.safe_load(fp)

API_KEYS = yconfig["api_keys"]
SEND_RATE_LIMIT = yconfig.get("send_rate_limit", 0)

# Start the pubsub process when the application starts
@asynccontextmanager
async def lifespan(app: FastAPI):
    app.wcf = Wcf(debug=True)
    app.wxid = app.wcf.get_self_wxid()
    contacts = app.wcf.query_sql("MicroMsg.db", "SELECT UserName, NickName FROM Contact;")
    app.contacts = {contact["UserName"]: contact["NickName"] for contact in contacts}

    print(f"Self wxid: {app.wxid}")
    print(f"Contacts: {app.contacts}")

    app.subscribers = set()

    def pubsub(wcf: Wcf):
        wcf.enable_receiving_msg(pyq=True)
        while wcf.is_receiving_msg():
            if not wcf.is_receiving_msg():
                print("WCF is not receiving messages")
                time.sleep(1)
                continue
            try:
                msg = wcf.get_msg()
                msg = {
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
                    "is_at": msg.is_at(app.wcf.self_wxid),
                    "is_self": msg.from_self(),
                    "is_group": msg.from_group(),
                }
                print(f"Received message: {json.dumps(msg, ensure_ascii=False)}")
                dead_subscribers = set()
                for subscriber in app.subscribers:
                    try:
                        subscriber(msg)
                    except Exception as e:
                        dead_subscribers.add(subscriber)
                        print(traceback.format_exc())
                        print(f"Error in subscriber: {e}")
                for dead in dead_subscribers:
                    app.subscribers.discard(dead)
            except Empty:
                time.sleep(1)
                continue
            except Exception as e:
                print(traceback.format_exc())
                print(f"Receiving message error: {e}")

    app.wcf.send_text("WCF HTTP 服务已启动", "filehelper")
    thread = threading.Thread(target=pubsub, args=(app.wcf,))
    thread.daemon = True
    thread.start()
    yield
    app.wcf.cleanup()


app = FastAPI(title="WCF HTTP", lifespan=lifespan)
# app = FastAPI(title="WCF HTTP")

async def verify_token(api_key: str = Security(APIKeyHeader(name="X-API-KEY", auto_error=False))):
    if api_key not in API_KEYS:
        raise HTTPException(
            status_code=HTTP_401_UNAUTHORIZED,
            detail="Invalid API Token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return True


@app.get("/", tags=["系统状态"])
def read_root(authenticated: bool = Depends(verify_token)):
    return {"message": "Service is running"}


# Message sending endpoints
@app.post("/send-text", tags=["消息发送"])
def send_text(
        msg: str = Body(description="要发送的消息，换行用\\n表示"),
        receiver: str = Body("filehelper", description="消息接收者，roomid 或者 wxid"),
        aters: str = Body("", description="要 @ 的 wxid，多个用逗号分隔；@所有人 用 notify@all"),
        authenticated: bool = Depends(verify_token),
):
    try:
        ret = app.wcf.send_text(msg, receiver, aters)
        return {"status": "ok", "data": ret}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.post("/send-image", tags=["消息发送"])
def send_image(
        image_data: str = Body(description="图片的base64编码数据"),
        filename: str = Body(description="图片文件名，包含扩展名"),
        receiver: str = Body(description="消息接收者，roomid 或者 wxid"),
        authenticated: bool = Depends(verify_token),
):
    try:
        # 解码base64数据
        image_bytes = base64.b64decode(image_data)
        
        # 创建临时文件保存图片
        temp_dir = tempfile.gettempdir()
        temp_path = os.path.join(temp_dir, filename)
        
        with open(temp_path, "wb") as buffer:
            buffer.write(image_bytes)
        
        # 发送图片
        ret = app.wcf.send_image(temp_path, receiver)
        
        return {"status": "ok", "data": ret}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.post("/send-file", tags=["消息发送"])
def send_file(
        file_data: str = Body(description="文件的base64编码数据"),
        filename: str = Body(description="文件名，包含扩展名"),
        receiver: str = Body(description="消息接收者，roomid 或者 wxid"),
        authenticated: bool = Depends(verify_token),
):
    try:
        # 解码base64数据
        file_bytes = base64.b64decode(file_data)
        
        # 创建临时文件保存文件
        temp_dir = tempfile.gettempdir()
        temp_path = os.path.join(temp_dir, filename)
        
        with open(temp_path, "wb") as buffer:
            buffer.write(file_bytes)
        
        # 发送文件
        ret = app.wcf.send_file(temp_path, receiver)
        
        return {"status": "ok", "data": ret}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.post("/send-emotion", tags=["消息发送"])
def send_emotion(
        file_data: str = Body(description="表情文件的base64编码数据"),
        filename: str = Body(description="表情文件名，包含扩展名"),
        receiver: str = Body(description="消息接收者，roomid 或者 wxid"),
        authenticated: bool = Depends(verify_token),
):
    try:
        # 解码base64数据
        file_bytes = base64.b64decode(file_data)
        
        # 创建临时文件保存表情
        temp_dir = tempfile.gettempdir()
        temp_path = os.path.join(temp_dir, filename)
        
        with open(temp_path, "wb") as buffer:
            buffer.write(file_bytes)
        
        # 发送表情
        ret = app.wcf.send_emotion(temp_path, receiver)
        
        return {"status": "ok", "data": ret}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.post("/send-rich-text", tags=["消息发送"])
def send_rich_text(
        name: str = Body(description="左下显示的名字"),
        account: str = Body(description="公众号id，可以显示对应的头像"),
        title: str = Body(description="标题，最多两行"),
        digest: str = Body(description="摘要，三行"),
        url: str = Body(description="点击后跳转的链接"),
        thumburl: str = Body(description="缩略图的链接"),
        receiver: str = Body(description="接收人, wxid 或者 roomid"),
        authenticated: bool = Depends(verify_token),
):
    try:
        ret = app.wcf.send_rich_text(name, account, title, digest, url, thumburl, receiver)
        return {"status": "ok", "data": ret}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.post("/send-pat-msg", tags=["消息发送"])
def send_pat(
        roomid: str = Body(description="群id"),
        wxid: str = Body(description="要拍的群友的wxid"),
        authenticated: bool = Depends(verify_token),
):
    try:
        ret = app.wcf.send_pat_msg(roomid, wxid)
        return {"status": "ok", "data": ret}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.post("/forward-msg", tags=["消息发送"])
def forward_msg(
        id: int = Body(description="消息中id"),
        thumb: str = Body(description="消息中的thumb"),
        extra: str = Body(description="消息中的extra"),
        authenticated: bool = Depends(verify_token),
):
    try:
        ret = app.wcf.forward_msg(id, thumb, extra)
        return {"status": "ok", "data": ret}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.post("/send-xml", tags=["消息发送"])
def send_xml(
        receiver: str = Body(description="消息接收人，wxid 或者 roomid"),
        xml: str = Body(description="xml 内容"),
        type: int = Body(description="xml 类型，如：0x21 为小程序"),
        cover_data: str = Body(None, description="封面图片的base64编码数据"),
        cover_filename: str = Body(None, description="封面图片文件名，包含扩展名"),
        authenticated: bool = Depends(verify_token),
):
    try:
        temp_path = None
        if cover_data and cover_filename:
            # 解码base64数据
            cover_bytes = base64.b64decode(cover_data)
            
            # 创建临时文件保存封面图片
            temp_dir = tempfile.gettempdir()
            temp_path = os.path.join(temp_dir, cover_filename)
            
            with open(temp_path, "wb") as buffer:
                buffer.write(cover_bytes)
        
        # 发送xml
        ret = app.wcf.send_xml(receiver, xml, type, temp_path)
        
        return {"status": "ok", "data": ret}
    except Exception as e:
        return {"status": "error", "message": str(e)}


# Friend and group management endpoints
@app.post("/accept-new-friend", tags=["好友和群组管理"])
def accept_friend(
        v3: str = Body(description="加密用户名，好友申请消息里v3开头的字符串"),
        v4: str = Body(description="Ticket，好友申请消息里v4开头的字符串"),
        scene: int = Body(30, description="申请方式，默认为扫码添加(30)"),
        authenticated: bool = Depends(verify_token),
):
    try:
        ret = app.wcf.accept_new_friend(v3, v4, scene)
        return {"status": "ok", "data": ret}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.post("/receive-transfer", tags=["好友和群组管理"])
def receive_transfer(
        wxid: str = Body(description="转账消息里的发送人wxid"),
        transferid: str = Body(description="转账消息里的transferid"),
        transactionid: str = Body(description="转账消息里的transactionid"),
        authenticated: bool = Depends(verify_token),
):
    try:
        ret = app.wcf.receive_transfer(wxid, transferid, transactionid)
        return {"status": "ok", "data": ret}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.post("/add-chatroom-members", tags=["好友和群组管理"])
def add_chatroom_members(
        roomid: str = Body(description="待加群的id"),
        wxids: str = Body(description="要加到群里的wxid，多个用逗号分隔"),
        authenticated: bool = Depends(verify_token),
):
    try:
        ret = app.wcf.add_chatroom_members(roomid, wxids)
        return {"status": "ok", "data": ret}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.post("/del-chatroom-members", tags=["好友和群组管理"])
def del_chatroom_members(
        roomid: str = Body(description="群的id"),
        wxids: str = Body(description="要删除成员的wxid，多个用逗号分隔"),
        authenticated: bool = Depends(verify_token),
):
    try:
        ret = app.wcf.del_chatroom_members(roomid, wxids)
        return {"status": "ok", "data": ret}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.post("/invite-chatroom-members", tags=["好友和群组管理"])
def invite_chatroom_members(
        roomid: str = Body(description="群的id"),
        wxids: str = Body(description="要邀请成员的wxid，多个用逗号分隔"),
        authenticated: bool = Depends(verify_token),
):
    try:
        ret = app.wcf.invite_chatroom_members(roomid, wxids)
        return {"status": "ok", "data": ret}
    except Exception as e:
        return {"status": "error", "message": str(e)}


# Media processing endpoints
@app.post("/download-attach", tags=["媒体处理"])
def download_attach(
        id: int = Body(description="消息中id"),
        thumb: str = Body(description="消息中的thumb"),
        extra: str = Body(description="消息中的extra"),
        authenticated: bool = Depends(verify_token),
):
    try:
        ret = app.wcf.download_attach(id, thumb, extra)
        return {"status": "ok", "data": ret}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.post("/download-image", tags=["媒体处理"])
def download_image(
        id: int = Body(description="消息中id"),
        extra: str = Body(description="消息中的extra"),
        dir: str = Body(description="存放图片的目录"),
        timeout: int = Body(30, description="超时时间（秒）"),
        authenticated: bool = Depends(verify_token),
):
    try:
        ret = app.wcf.download_image(id, extra, dir, timeout)
        return {"status": "ok", "data": ret}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.post("/download-video", tags=["媒体处理"])
def download_video(
        id: int = Body(description="消息中id"),
        thumb: str = Body(description="消息中的thumb"),
        dir: str = Body(description="存放视频的目录"),
        timeout: int = Body(30, description="超时时间（秒）"),
        authenticated: bool = Depends(verify_token),
):
    try:
        ret = app.wcf.download_video(id, thumb, dir, timeout)
        return {"status": "ok", "data": ret}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.post("/decrypt-image", tags=["媒体处理"])
def decrypt_image(
        src: str = Body(description="加密的图片路径"),
        dir: str = Body(description="保存图片的目录"),
        authenticated: bool = Depends(verify_token),
):
    try:
        ret = app.wcf.decrypt_image(src, dir)
        return {"status": "ok", "data": ret}
    except Exception as e:
        return {"status": "error", "message": str(e)}


# Information retrieval endpoints
@app.get("/get-chatroom-members", tags=["信息获取"])
def get_chatroom_members(
        roomid: str,
        authenticated: bool = Depends(verify_token),
):
    try:
        ret = app.wcf.get_chatroom_members(roomid)
        return {"status": "ok", "data": ret}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.get("/get-alias-in-chatroom", tags=["信息获取"])
def get_alias_in_chatroom(
        wxid: str,
        roomid: str,
        authenticated: bool = Depends(verify_token),
):
    try:
        ret = app.wcf.get_alias_in_chatroom(wxid, roomid)
        return {"status": "ok", "data": ret}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.get("/get-friends", tags=["信息获取"])
def get_friends(
        authenticated: bool = Depends(verify_token),
):
    try:
        ret = app.wcf.get_friends()
        return {"status": "ok", "data": ret}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.get("/get-contacts", tags=["信息获取"])
def get_contacts(
        authenticated: bool = Depends(verify_token),
):
    try:
        ret = app.wcf.get_contacts()
        return {"status": "ok", "data": ret}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.get("/get-dbs", tags=["信息获取"])
def get_dbs(
        authenticated: bool = Depends(verify_token),
):
    try:
        ret = app.wcf.get_dbs()
        return {"status": "ok", "data": ret}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.get("/get-tables", tags=["信息获取"])
def get_tables(
        db: str,
        authenticated: bool = Depends(verify_token),
):
    try:
        ret = app.wcf.get_tables(db)
        return {"status": "ok", "data": ret}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.get("/get-user-info", tags=["信息获取"])
def get_user_info(
        authenticated: bool = Depends(verify_token),
):
    try:
        ret = app.wcf.get_user_info()
        return {"status": "ok", "data": ret}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.get("/get-msg-types", tags=["信息获取"])
def get_msg_types(
        authenticated: bool = Depends(verify_token),
):
    try:
        ret = app.wcf.get_msg_types()
        return {"status": "ok", "data": ret}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.get("/get-qrcode", tags=["信息获取"])
def get_qrcode(
        authenticated: bool = Depends(verify_token),
):
    try:
        ret = app.wcf.get_qrcode()
        return {"status": "ok", "data": ret}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.get("/get-info-by-wxid", tags=["信息获取"])
def get_info_by_wxid(
        wxid: str,
        authenticated: bool = Depends(verify_token),
):
    try:
        ret = app.wcf.get_info_by_wxid(wxid)
        return {"status": "ok", "data": ret}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.post("/get-ocr-result", tags=["信息获取"])
def get_ocr_result(
        extra: str = Body(description="待识别的图片路径，消息里的extra"),
        timeout: int = Body(2, description="超时时间（秒）"),
        authenticated: bool = Depends(verify_token),
):
    try:
        ret = app.wcf.get_ocr_result(extra, timeout)
        return {"status": "ok", "data": ret}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.get("/get-room-name", tags=["信息获取"])
def get_room_name(
        roomid: str,
        authenticated: bool = Depends(verify_token),
):
    try:
        ret = app.wcf.get_room_name(roomid)
        return {"status": "ok", "data": ret}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.get("/get-room-wxids", tags=["信息获取"])
def get_room_wxids(
        roomid: str,
        authenticated: bool = Depends(verify_token),
):
    try:
        ret = app.wcf.get_room_wxids(roomid)
        return {"status": "ok", "data": ret}
    except Exception as e:
        return {"status": "error", "message": str(e)}


# Message management endpoints
@app.post("/revoke-msg", tags=["消息管理"])
def revoke_msg(
        id: int = Body(description="待撤回消息的id"),
        authenticated: bool = Depends(verify_token),
):
    try:
        ret = app.wcf.revoke_msg(id)
        return {"status": "ok", "data": ret}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.post("/enable-receiving-msg", tags=["消息管理"])
def enable_receiving_msg(
        pyq: bool = Body(False, description="是否接收朋友圈消息"),
        authenticated: bool = Depends(verify_token),
):
    try:
        ret = app.wcf.enable_receiving_msg(pyq)
        return {"status": "ok", "data": ret}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.post("/disable-recv-msg", tags=["消息管理"])
def disable_receiving_msg(
        authenticated: bool = Depends(verify_token),
):
    try:
        ret = app.wcf.disable_recv_msg()
        return {"status": "ok", "data": ret}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.get("/is-receiving-msg", tags=["消息管理"])
def is_receiving_msg(
        authenticated: bool = Depends(verify_token),
):
    try:
        ret = app.wcf.is_receiving_msg()
        return {"status": "ok", "data": ret}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.get("/get-msg", tags=["消息管理"])
def get_msg(
        block: bool = True,
        authenticated: bool = Depends(verify_token),
):
    try:
        ret = app.wcf.get_msg(block)
        return {"status": "ok", "data": ret}
    except Exception as e:
        return {"status": "error", "message": str(e)}


# System status endpoints
@app.get("/is-login", tags=["系统状态"])
def is_login(
        authenticated: bool = Depends(verify_token),
):
    try:
        ret = app.wcf.is_login()
        return {"status": "ok", "data": ret}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.get("/get-self-wxid", tags=["系统状态"])
def get_self_wxid(
        authenticated: bool = Depends(verify_token),
):
    try:
        ret = app.wcf.get_self_wxid()
        return {"status": "ok", "data": ret}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.post("/query-sql", tags=["系统状态"])
def query_sql(
        db: str = Body(description="要查询的数据库"),
        sql: str = Body(description="要执行的SQL"),
        authenticated: bool = Depends(verify_token),
):
    try:
        ret = app.wcf.query_sql(db, sql)
        return {"status": "ok", "data": ret}
    except Exception as e:
        return {"status": "error", "message": str(e)}


# Pyq related endpoints
@app.post("/refresh-pyq", tags=["朋友圈相关"])
def refresh_pyq(
        id: int = Body(0, description="开始id，0为最新页"),
        authenticated: bool = Depends(verify_token),
):
    try:
        ret = app.wcf.refresh_pyq(id)
        return {"status": "ok", "data": ret}
    except Exception as e:
        return {"status": "error", "message": str(e)}


# Message subscription endpoints
@app.get("/subscribe", tags=["消息订阅"])
def subscribe(request: Request, authenticated: bool = Depends(verify_token)):
    msg_queue = asyncio.Queue()

    def subscriber(msg: WxMsg):
        msg_queue.put_nowait(msg)

    app.subscribers.add(subscriber)

    async def event_generator():
        try:
            while True:
                if await request.is_disconnected():
                    break
                msg = await msg_queue.get()
                yield {
                    "event": "message",
                    "data": json.dumps(msg, ensure_ascii=False),
                }
        except Exception as e:
            print(f"Client disconnected: {e}")
        finally:
            app.subscribers.discard(subscriber)

    return EventSourceResponse(event_generator())


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000)
