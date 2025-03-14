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
import sys
from queue import Empty

import uvicorn
import yaml
from fastapi import Body, Request, FastAPI, HTTPException, Security, Depends, UploadFile, File
from fastapi.security import APIKeyHeader
from sse_starlette.sse import EventSourceResponse
from starlette.status import HTTP_401_UNAUTHORIZED
from contextlib import asynccontextmanager

from wcferry import Wcf, WxMsg

sys.stdout.reconfigure(encoding='utf-8')

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
    """检查服务是否正在运行"""
    return {"message": "Service is running"}


# Message sending endpoints
@app.post("/send-text", tags=["消息发送"])
def send_text(
        msg: str = Body(description="要发送的消息，换行用\\n表示"),
        receiver: str = Body("filehelper", description="消息接收者，roomid 或者 wxid"),
        aters: str = Body("", description="要 @ 的 wxid，多个用逗号分隔；@所有人 用 notify@all"),
        authenticated: bool = Depends(verify_token),
):
    """发送文本消息
    
    Args:
        msg: 要发送的消息，换行使用 \\n
        receiver: 消息接收人，wxid 或者 roomid
        aters: 要 @ 的 wxid，多个用逗号分隔；@所有人 只需要 notify@all
        
    Returns:
        dict: {"status": "ok", "data": ret} 或 {"status": "error", "message": str}
        ret: 0 为成功，其他失败
    """
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
    """发送图片
    
    Args:
        image_data: 图片的base64编码数据
        filename: 图片文件名，包含扩展名
        receiver: 消息接收人，wxid 或者 roomid
        
    Returns:
        dict: {"status": "ok", "data": ret} 或 {"status": "error", "message": str}
        ret: 0 为成功，其他失败
    """
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
    """发送文件
    
    Args:
        file_data: 文件的base64编码数据
        filename: 文件名，包含扩展名
        receiver: 消息接收人，wxid 或者 roomid
        
    Returns:
        dict: {"status": "ok", "data": ret} 或 {"status": "error", "message": str}
        ret: 0 为成功，其他失败
    """
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
    """发送表情
    
    Args:
        file_data: 表情文件的base64编码数据
        filename: 表情文件名，包含扩展名
        receiver: 消息接收人，wxid 或者 roomid
        
    Returns:
        dict: {"status": "ok", "data": ret} 或 {"status": "error", "message": str}
        ret: 0 为成功，其他失败
    """
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
    """发送富文本消息
    
    卡片样式：
        |-------------------------------------|
        |title, 最长两行
        |(长标题, 标题短的话这行没有)
        |digest, 最多三行，会占位    |--------|
        |digest, 最多三行，会占位    |thumburl|
        |digest, 最多三行，会占位    |--------|
        |(account logo) name
        |-------------------------------------|
    
    Args:
        name: 左下显示的名字
        account: 填公众号 id 可以显示对应的头像（gh_ 开头的）
        title: 标题，最多两行
        digest: 摘要，三行
        url: 点击后跳转的链接
        thumburl: 缩略图的链接
        receiver: 接收人, wxid 或者 roomid
        
    Returns:
        dict: {"status": "ok", "data": ret} 或 {"status": "error", "message": str}
        ret: 0 为成功，其他失败
    """
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
    """拍一拍群友
    
    Args:
        roomid: 群 id
        wxid: 要拍的群友的 wxid
        
    Returns:
        dict: {"status": "ok", "data": ret} 或 {"status": "error", "message": str}
        ret: 1 为成功，其他失败
    """
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
    """转发消息。可以转发文本、图片、表情、甚至各种 XML；语音也行，不过效果嘛，自己验证吧。
    
    Args:
        id: 待转发消息的 id
        thumb: 消息中的 thumb
        extra: 消息中的 extra
        
    Returns:
        dict: {"status": "ok", "data": ret} 或 {"status": "error", "message": str}
        ret: 1 为成功，其他失败
    """
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
    """发送 XML
    
    Args:
        receiver: 消息接收人，wxid 或者 roomid
        xml: xml 内容
        type: xml 类型，如：0x21 为小程序
        cover_data: 封面图片的base64编码数据
        cover_filename: 封面图片文件名，包含扩展名
        
    Returns:
        dict: {"status": "ok", "data": ret} 或 {"status": "error", "message": str}
        ret: 0 为成功，其他失败
    """
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
    """通过好友申请
    
    Args:
        v3: 加密用户名 (好友申请消息里 v3 开头的字符串)
        v4: Ticket (好友申请消息里 v4 开头的字符串)
        scene: 申请方式 (好友申请消息里的 scene)；为了兼容旧接口，默认为扫码添加 (30)
        
    Returns:
        dict: {"status": "ok", "data": ret} 或 {"status": "error", "message": str}
        ret: 1 为成功，其他失败
    """
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
    """接收转账
    
    Args:
        wxid: 转账消息里的发送人 wxid
        transferid: 转账消息里的 transferid
        transactionid: 转账消息里的 transactionid
        
    Returns:
        dict: {"status": "ok", "data": ret} 或 {"status": "error", "message": str}
        ret: 1 为成功，其他失败
    """
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
    """添加群成员
    
    Args:
        roomid: 待加群的 id
        wxids: 要加到群里的 wxid，多个用逗号分隔
        
    Returns:
        dict: {"status": "ok", "data": ret} 或 {"status": "error", "message": str}
        ret: 1 为成功，其他失败
    """
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
    """删除群成员
    
    Args:
        roomid: 群的 id
        wxids: 要删除成员的 wxid，多个用逗号分隔
        
    Returns:
        dict: {"status": "ok", "data": ret} 或 {"status": "error", "message": str}
        ret: 1 为成功，其他失败
    """
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
    """邀请群成员
    
    Args:
        roomid: 群的 id
        wxids: 要邀请成员的 wxid, 多个用逗号`,`分隔
        
    Returns:
        dict: {"status": "ok", "data": ret} 或 {"status": "error", "message": str}
        ret: 1 为成功，其他失败
    """
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
    """下载附件（图片、视频、文件）。这方法别直接调用，下载图片使用 `download_image`。
    
    Args:
        id: 消息中 id
        thumb: 消息中的 thumb
        extra: 消息中的 extra
        
    Returns:
        dict: {"status": "ok", "data": ret} 或 {"status": "error", "message": str}
        ret: 成功返回字典包含文件名和base64编码内容；失败返回空字符串，原因见日志
    """
    try:
        result = app.wcf.download_attach(id, thumb, extra)
        if result == 0:  # 成功
            # 尝试从extra中获取文件路径
            if os.path.exists(extra):
                # 如果文件存在，读取文件并转为base64
                with open(extra, "rb") as f:
                    file_content = f.read()

                # 获取文件名
                file_name = os.path.basename(extra)

                # 转为base64
                base64_content = base64.b64encode(file_content).decode('utf-8')

                # 返回文件名和base64内容
                return {"status": "ok", "data": {"file_name": file_name, "base64_content": base64_content}}
            return {"status": "ok", "data": {"status": "downloaded", "path": extra}}
        else:
            return {"status": "ok", "data": ""}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.post("/download-image", tags=["媒体处理"])
def download_image(
        id: int = Body(description="消息中id"),
        extra: str = Body(description="消息中的extra"),
        timeout: int = Body(30, description="超时时间（秒）"),
        authenticated: bool = Depends(verify_token),
):
    """下载图片
    
    Args:
        id: 消息中 id
        extra: 消息中的 extra
        timeout: 超时时间（秒）
        
    Returns:
        dict: {"status": "ok", "data": ret} 或 {"status": "error", "message": str}
        ret: 成功返回字典包含文件名和base64编码内容；失败返回空字符串，原因见日志
    """
    try:
        # 使用临时目录
        temp_dir = tempfile.gettempdir()

        file_path = app.wcf.download_image(id, extra, temp_dir, timeout)
        if file_path:
            # 如果下载成功，读取文件并转为base64
            with open(file_path, "rb") as f:
                file_content = f.read()

            # 获取文件名
            file_name = os.path.basename(file_path)

            # 转为base64
            base64_content = base64.b64encode(file_content).decode('utf-8')

            # 返回文件名和base64内容
            return {"status": "ok", "data": {"file_name": file_name, "base64_content": base64_content}}
        else:
            return {"status": "ok", "data": ""}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.post("/download-video", tags=["媒体处理"])
def download_video(
        id: int = Body(description="消息中id"),
        thumb: str = Body(description="消息中的thumb"),
        timeout: int = Body(30, description="超时时间（秒）"),
        authenticated: bool = Depends(verify_token),
):
    """下载视频
    
    Args:
        id: 消息中 id
        thumb: 消息中的 thumb（即视频的封面图）
        timeout: 超时时间（秒）
        
    Returns:
        dict: {"status": "ok", "data": ret} 或 {"status": "error", "message": str}
        ret: 成功返回字典包含文件名和base64编码内容；失败返回空字符串，原因见日志
    """
    try:
        # 使用临时目录
        temp_dir = tempfile.gettempdir()

        file_path = app.wcf.download_video(id, thumb, temp_dir, timeout)
        if file_path:
            # 如果下载成功，读取文件并转为base64
            with open(file_path, "rb") as f:
                file_content = f.read()

            # 获取文件名
            file_name = os.path.basename(file_path)

            # 转为base64
            base64_content = base64.b64encode(file_content).decode('utf-8')

            # 返回文件名和base64内容
            return {"status": "ok", "data": {"file_name": file_name, "base64_content": base64_content}}
        else:
            return {"status": "ok", "data": ""}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.post("/decrypt-image", tags=["媒体处理"])
def decrypt_image(
        src: str = Body(description="加密的图片路径"),
        authenticated: bool = Depends(verify_token),
):
    """解密图片。这方法别直接调用，下载图片使用 `download_image`。
    
    Args:
        src: 加密的图片路径
        
    Returns:
        dict: {"status": "ok", "data": ret} 或 {"status": "error", "message": str}
        ret: 成功返回字典包含文件名和base64编码内容；失败返回空字符串，原因见日志
    """
    try:
        # 使用临时目录
        temp_dir = tempfile.gettempdir()

        file_path = app.wcf.decrypt_image(src, temp_dir)
        if file_path:
            # 如果解密成功，读取文件并转为base64
            with open(file_path, "rb") as f:
                file_content = f.read()

            # 获取文件名
            file_name = os.path.basename(file_path)

            # 转为base64
            base64_content = base64.b64encode(file_content).decode('utf-8')

            # 返回文件名和base64内容
            return {"status": "ok", "data": {"file_name": file_name, "base64_content": base64_content}}
        else:
            return {"status": "ok", "data": ""}
    except Exception as e:
        return {"status": "error", "message": str(e)}


# Information retrieval endpoints
@app.get("/get-chatroom-members", tags=["信息获取"])
def get_chatroom_members(
        roomid: str,
        authenticated: bool = Depends(verify_token),
):
    """获取群成员
    
    Args:
        roomid: 群的 id
        
    Returns:
        dict: {"status": "ok", "data": ret} 或 {"status": "error", "message": str}
        ret: 群成员列表: {wxid1: 昵称1, wxid2: 昵称2, ...}
    """
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
    """获取群名片
    
    Args:
        wxid: wxid
        roomid: 群的 id
        
    Returns:
        dict: {"status": "ok", "data": ret} 或 {"status": "error", "message": str}
        ret: 群名片
    """
    try:
        ret = app.wcf.get_alias_in_chatroom(wxid, roomid)
        return {"status": "ok", "data": ret}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.get("/get-friends", tags=["信息获取"])
def get_friends(
        authenticated: bool = Depends(verify_token),
):
    """获取好友列表
    
    Returns:
        dict: {"status": "ok", "data": ret} 或 {"status": "error", "message": str}
        ret: 好友列表
    """
    try:
        ret = app.wcf.get_friends()
        return {"status": "ok", "data": ret}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.get("/get-contacts", tags=["信息获取"])
def get_contacts(
        authenticated: bool = Depends(verify_token),
):
    """获取完整通讯录
    
    Returns:
        dict: {"status": "ok", "data": ret} 或 {"status": "error", "message": str}
        ret: 通讯录列表
    """
    try:
        ret = app.wcf.get_contacts()
        return {"status": "ok", "data": ret}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.get("/get-dbs", tags=["信息获取"])
def get_dbs(
        authenticated: bool = Depends(verify_token),
):
    """获取所有数据库
    
    Returns:
        dict: {"status": "ok", "data": ret} 或 {"status": "error", "message": str}
        ret: 数据库列表
    """
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
    """获取 db 中所有表
    
    Args:
        db: 数据库名（可通过 `get_dbs` 查询）
        
    Returns:
        dict: {"status": "ok", "data": ret} 或 {"status": "error", "message": str}
        ret: `db` 下的所有表名及对应建表语句
    """
    try:
        ret = app.wcf.get_tables(db)
        return {"status": "ok", "data": ret}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.get("/get-user-info", tags=["信息获取"])
def get_user_info(
        authenticated: bool = Depends(verify_token),
):
    """获取登录账号个人信息
    
    Returns:
        dict: {"status": "ok", "data": ret} 或 {"status": "error", "message": str}
        ret: 用户信息
    """
    try:
        ret = app.wcf.get_user_info()
        return {"status": "ok", "data": ret}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.get("/get-msg-types", tags=["信息获取"])
def get_msg_types(
        authenticated: bool = Depends(verify_token),
):
    """获取所有消息类型
    
    Returns:
        dict: {"status": "ok", "data": ret} 或 {"status": "error", "message": str}
        ret: 消息类型字典
    """
    try:
        ret = app.wcf.get_msg_types()
        return {"status": "ok", "data": ret}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.get("/get-qrcode", tags=["信息获取"])
def get_qrcode(
        authenticated: bool = Depends(verify_token),
):
    """获取登录二维码，已经登录则返回空字符串
    
    Returns:
        dict: {"status": "ok", "data": ret} 或 {"status": "error", "message": str}
        ret: 登录二维码
    """
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
    """通过 wxid 查询微信号昵称等信息
    
    Args:
        wxid: 联系人 wxid
        
    Returns:
        dict: {"status": "ok", "data": ret} 或 {"status": "error", "message": str}
        ret: {wxid, code, name, gender}
    """
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
    """获取 OCR 结果。鸡肋，需要图片能自动下载；通过下载接口下载的图片无法识别。
    
    Args:
        extra: 待识别的图片路径，消息里的 extra
        timeout: 超时时间（秒）
        
    Returns:
        dict: {"status": "ok", "data": ret} 或 {"status": "error", "message": str}
        ret: OCR 结果
    """
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
    """获取群名称
    
    Args:
        roomid: 群的 id
        
    Returns:
        dict: {"status": "ok", "data": ret} 或 {"status": "error", "message": str}
        ret: 群名称
    """
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
    """获取群成员 wxid 列表
    
    Args:
        roomid: 群的 id
        
    Returns:
        dict: {"status": "ok", "data": ret} 或 {"status": "error", "message": str}
        ret: 群成员 wxid 列表
    """
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
    """撤回消息
    
    Args:
        id: 待撤回消息的 id
        
    Returns:
        dict: {"status": "ok", "data": ret} 或 {"status": "error", "message": str}
        ret: 1 为成功，其他失败
    """
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
    """允许接收消息，成功后通过 `get_msg` 读取消息
    
    Args:
        pyq: 是否接收朋友圈消息
        
    Returns:
        dict: {"status": "ok", "data": ret} 或 {"status": "error", "message": str}
        ret: True 为成功，False 为失败
    """
    try:
        ret = app.wcf.enable_receiving_msg(pyq)
        return {"status": "ok", "data": ret}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.post("/disable-recv-msg", tags=["消息管理"])
def disable_receiving_msg(
        authenticated: bool = Depends(verify_token),
):
    """停止接收消息
    
    Returns:
        dict: {"status": "ok", "data": ret} 或 {"status": "error", "message": str}
        ret: 0 为成功，其他失败
    """
    try:
        ret = app.wcf.disable_recv_msg()
        return {"status": "ok", "data": ret}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.get("/is-receiving-msg", tags=["消息管理"])
def is_receiving_msg(
        authenticated: bool = Depends(verify_token),
):
    """是否已启动接收消息功能
    
    Returns:
        dict: {"status": "ok", "data": ret} 或 {"status": "error", "message": str}
        ret: True 为已启动，False 为未启动
    """
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
    """从消息队列中获取消息
    
    Args:
        block: 是否阻塞，默认阻塞
        
    Returns:
        dict: {"status": "ok", "data": ret} 或 {"status": "error", "message": str}
        ret: 微信消息
    """
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
    """是否已经登录
    
    Returns:
        dict: {"status": "ok", "data": ret} 或 {"status": "error", "message": str}
        ret: True 为已登录，False 为未登录
    """
    try:
        ret = app.wcf.is_login()
        return {"status": "ok", "data": ret}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.get("/get-self-wxid", tags=["系统状态"])
def get_self_wxid(
        authenticated: bool = Depends(verify_token),
):
    """获取登录账户的 wxid
    
    Returns:
        dict: {"status": "ok", "data": ret} 或 {"status": "error", "message": str}
        ret: 登录账户的 wxid
    """
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
    """执行 SQL，如果数据量大注意分页，以免 OOM
    
    Args:
        db: 要查询的数据库
        sql: 要执行的 SQL
        
    Returns:
        dict: {"status": "ok", "data": ret} 或 {"status": "error", "message": str}
        ret: 查询结果
    """
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
    """刷新朋友圈
    
    Args:
        id: 开始 id，0 为最新页
        
    Returns:
        dict: {"status": "ok", "data": ret} 或 {"status": "error", "message": str}
        ret: 1 为成功，其他失败
    """
    try:
        ret = app.wcf.refresh_pyq(id)
        return {"status": "ok", "data": ret}
    except Exception as e:
        return {"status": "error", "message": str(e)}


# Message subscription endpoints
@app.get("/subscribe", tags=["消息订阅"])
def subscribe(request: Request, authenticated: bool = Depends(verify_token)):
    """订阅消息，使用 Server-Sent Events (SSE) 推送消息
    
    Returns:
        EventSourceResponse: SSE 响应，推送消息事件
    """
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
