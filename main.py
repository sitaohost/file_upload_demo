from fastapi import FastAPI, File, UploadFile, HTTPException, Body, Query
from fastapi.responses import HTMLResponse
import uvicorn
from typing import Annotated
from fastapi.staticfiles import StaticFiles
from pathlib import Path
import os
import aiofiles  # 异步文件操作库
import asyncio  # 异步库
from urllib.parse import unquote
from fastapi.middleware.cors import CORSMiddleware  # 导入跨域中间件
from fastapi.responses import StreamingResponse

app = FastAPI()
app.mount("/static", StaticFiles(directory="static/public"), name="static")

project_root = Path(__file__).resolve().parent  # 项目根目录
STATIC_DIR = project_root / "static"  # 静态文件根目录
PUBLIC_DIR = STATIC_DIR / "public"  # 公开文件目录
PRIVATE_DIR = STATIC_DIR / "private"  # 私有文件目录

# 确保目录存在
os.makedirs(PUBLIC_DIR, exist_ok=True)
os.makedirs(PRIVATE_DIR, exist_ok=True)

chunk_size = 256 * 1024  # 分块大小: 256KB


def _format_size(size):
    """格式化文件大小"""
    if size < 1024:
        return f"{size}B"
    elif size < 1024 * 1024:
        return f"{size / 1024:.2f}KB"
    return f"{size / 1024 / 1024:.2f}MB"


# 一个简单的上传文件表单
@app.get("/uploadform/", response_class=HTMLResponse)
async def upload_form():
    return """
    <form action="/upload/" enctype="multipart/form-data" method="post">
        <input name="files" type="file" multiple>
        <input type="submit">
    </form>
    """


@app.post("/upload/")
async def create_upload_files(
    files: Annotated[list[UploadFile], File(...)], is_shared: bool = False
):
    results = []

    for file in files:
        file_save_path = (
            PUBLIC_DIR / file.filename if is_shared else PRIVATE_DIR / file.filename
        )

        # 异步分块保存文件
        async with aiofiles.open(file_save_path, "wb") as buffer:
            while chunk := await file.read(chunk_size):
                await buffer.write(chunk)

        file_size = _format_size(file.size)

        results.append(
            {
                "filename": file.filename,
                "content_type": file.content_type,
                "file_size": file_size,
            }
        )

    return {"message": f"成功上传 {len(files)} 个文件", "files": results}


def _get_file_info(file_path: Path, base_dir: Path):
    """获取单个文件/文件夹的详细信息"""
    stat = file_path.stat()
    is_shared = base_dir == PUBLIC_DIR
    return {
        "name": file_path.name,
        "size": _format_size(stat.st_size) if file_path.is_file() else None,
        "is_dir": file_path.is_dir(),
        "is_shared": is_shared,
        "created": stat.st_ctime,
        "modified": stat.st_mtime,
        "children": _scan_directory(file_path) if file_path.is_dir() else None,
    }


def _scan_directory(directory: Path):
    """递归扫描目录"""
    result = []
    for item in directory.iterdir():
        result.append(_get_file_info(item, directory))
    return result


async def search_files(keyword: str, base_dir: Path, start_dir: Path = None):
    """搜索文件
    keyword: 搜索关键词
    base_dir: 基础目录(PUBLIC_DIR或PRIVATE_DIR)
    start_dir: 开始搜索的目录，默认为base_dir
    """
    if start_dir is None:
        start_dir = base_dir

    results = []

    # 使用异步方式递归搜索文件
    async def search_recursive(directory: Path):
        for item in directory.iterdir():
            # 如果文件名包含关键词，添加到结果中
            if keyword.lower() in item.name.lower():
                results.append(_get_file_info(item, base_dir))

            # 如果是目录，递归搜索
            if item.is_dir():
                await search_recursive(item)

    await search_recursive(start_dir)

    return {
        "search_keyword": keyword,
        "results_count": len(results),
        "results": results,
    }


@app.get("/files/")
async def list_files(path: str = "", is_shared: bool = False, search: str = None):
    """获取文件树
    path: 相对路径(用于获取子目录内容)
    is_shared: 是否获取公开目录
    search: 搜索关键词(按文件名搜索)
    """
    base_dir = PUBLIC_DIR if is_shared else PRIVATE_DIR
    target_dir = (base_dir / path).resolve()

    # 安全检查: 确保路径在base_dir下
    try:
        target_dir.relative_to(base_dir)
    except ValueError:
        raise HTTPException(status_code=403, detail="无权访问该路径")

    if not target_dir.exists():
        raise HTTPException(status_code=404, detail="路径不存在")

    # 如果有搜索关键词，执行搜索
    if search:
        return await search_files(search, base_dir, target_dir)

    return _get_file_info(target_dir, base_dir)


@app.post("/files/share")
async def toggle_share(data: dict = Body(...)):
    # filename: str, shared: bool
    filename = data["filename"]
    shared = data["shared"]
    # filename: str, shared: bool
    # 源路径和目标路径
    src = PRIVATE_DIR / filename if shared else PUBLIC_DIR / filename
    dst = PUBLIC_DIR / filename if shared else PRIVATE_DIR / filename

    if not src.exists():
        raise HTTPException(status_code=404, detail="文件不存在")

    # 移动文件
    src.rename(dst)

    return {"filename": filename, "is_shared": shared}


@app.get("/download/{filename}/")
async def download_file(filename: str):
    # 对URL中的文件名进行解码，处理可能的URL编码问题
    decoded_filename = unquote(filename)

    # 先在公开目录查找
    file_path = PUBLIC_DIR / decoded_filename
    if not file_path.exists():
        # 再在私有目录查找(如果允许的话)
        file_path = PRIVATE_DIR / decoded_filename
        if not file_path.exists():
            raise HTTPException(status_code=404, detail="文件不存在")

    # 获取文件MIME类型
    import mimetypes

    content_type, _ = mimetypes.guess_type(str(file_path))
    content_type = content_type or "application/octet-stream"

    # 定义异步文件读取生成器
    async def file_iterator():
        async with aiofiles.open(file_path, "rb") as f:
            while chunk := await f.read(chunk_size):
                yield chunk

    # 设置响应头，支持中文文件名
    from fastapi.responses import StreamingResponse
    from urllib.parse import quote

    # 确保文件名正确编码
    filename_encoded = quote(decoded_filename)

    # 使用RFC 6266标准的格式设置Content-Disposition头
    headers = {
        "Content-Disposition": f"attachment; filename=\"{filename_encoded}\"; filename*=UTF-8''{filename_encoded}",
        "Content-Type": content_type,
    }

    return StreamingResponse(file_iterator(), headers=headers, media_type=content_type)


@app.post("/folders/")
async def create_folder(new_folder=Body(...)):
    # folder_name: str, is_shared: bool = False
    folder_name = new_folder["folder_name"]
    is_shared = new_folder["is_shared"]

    """创建新文件夹"""
    target_dir = PUBLIC_DIR if is_shared else PRIVATE_DIR
    new_folder = target_dir / folder_name

    if new_folder.exists():
        raise HTTPException(status_code=400, detail="文件夹已存在")

    new_folder.mkdir()
    return {
        "message": "文件夹创建成功",
        "folder_name": folder_name,
        "is_shared": is_shared,
        "path": str(new_folder.relative_to(project_root)),
    }


@app.delete("/files/")
async def delete_file(file_path: str, is_shared: bool = False):
    """删除文件"""
    # file_path = data["file_path"]
    # is_shared = data.get("is_shared", False)

    target_dir = PUBLIC_DIR if is_shared else PRIVATE_DIR
    file_to_delete = target_dir / file_path

    # 检查文件是否存在
    if not file_to_delete.exists():
        raise HTTPException(status_code=404, detail="文件不存在")
    if file_to_delete.is_dir():
        raise HTTPException(status_code=400, detail="指定的路径是文件夹，不是文件")

    # 删除文件
    file_to_delete.unlink()

    return {"message": "文件删除成功", "file_path": file_path, "is_shared": is_shared}


@app.delete("/folders/")
async def delete_folder(data: dict = Body(...)):
    """删除文件夹"""
    folder_path = data["folder_path"]
    is_shared = data.get("is_shared", False)
    print(folder_path)
    target_dir = PUBLIC_DIR if is_shared else PRIVATE_DIR
    try:
        folder_to_delete = (target_dir / folder_path).resolve()
        print(folder_to_delete)
        # 安全检查: 确保路径在target_dir下
        folder_to_delete.relative_to(target_dir)
    except ValueError:
        raise HTTPException(status_code=403, detail="无权访问该路径")

    if not folder_to_delete.exists():
        raise HTTPException(status_code=404, detail="文件夹不存在")

    if not folder_to_delete.is_dir():
        raise HTTPException(status_code=400, detail="指定的路径不是文件夹")

    # 异步删除文件夹内容
    async def remove_path(path: Path):
        if path.is_file():
            await asyncio.to_thread(path.unlink)
        else:
            for item in path.iterdir():
                await remove_path(item)
            await asyncio.to_thread(path.rmdir)

    await remove_path(folder_to_delete)

    return {"message": "文件夹删除成功"}


@app.put("/files/")
async def rename_file(data: dict = Body(...)):
    """重命名文件"""
    old_path = data["old_path"]
    new_name = data["new_name"]

    """重命名文件"""
    # 先在公开目录查找
    src = PUBLIC_DIR / old_path
    if not src.exists():
        # 再在私有目录查找
        src = PRIVATE_DIR / old_path
        if not src.exists():
            raise HTTPException(status_code=404, detail="文件不存在")

    dst = src.parent / new_name
    if dst.exists():
        raise HTTPException(status_code=400, detail="目标文件名已存在")

    src.rename(dst)
    return {
        "message": "文件重命名成功",
        "old_path": old_path,
        "new_name": new_name,
        "is_shared": src.parent == PUBLIC_DIR,
    }


@app.put("/folders/")
async def rename_folder(data: dict = Body(...)):
    """重命名文件夹"""
    old_path = data["old_path"]
    new_name = data["new_name"]
    is_shared = data.get("is_shared", False)

    target_dir = PUBLIC_DIR if is_shared else PRIVATE_DIR
    src = target_dir / old_path

    # 检查源文件夹是否存在
    if not src.exists():
        raise HTTPException(status_code=404, detail="文件夹不存在")
    if not src.is_dir():
        raise HTTPException(status_code=400, detail="指定的路径不是文件夹")

    # 构建新路径
    dst = src.parent / new_name

    # 检查目标是否已存在
    if dst.exists():
        raise HTTPException(status_code=400, detail="目标文件夹名已存在")

    # 重命名文件夹
    src.rename(dst)

    return {
        "message": "文件夹重命名成功",
        "old_path": old_path,
        "new_name": new_name,
        "is_shared": is_shared,
        "new_path": str(dst.relative_to(target_dir)),
    }


app.add_middleware(
    CORSMiddleware,
    # allow_origins=["http://localhost:5173", "http://localhost:5174"], # 前端开发服务器端口
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
def hello():
    return {"message": "一个简单的 FastAPI 文件上传Demo"}


if __name__ == "__main__":
    current_path = Path(__file__).parent
    print(project_root)
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True, workers=2)
