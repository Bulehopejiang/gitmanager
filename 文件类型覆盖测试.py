"""全类型覆盖测试：常见文件类型 + 特殊文件名 的变更/差异支持情况。

覆盖：
  文本类：.py .c .h .v .sv .txt .md .json .xml .yaml .ini .cfg .csv .tcl .asm .mif .qsf .sh .bat .log
  二进制类：.docx .xlsx .pdf .png .zip .vsdx .exe .pyc
  特殊名：中文名、含空格、含 # & % + ' ( ) [ ]、多层目录、无扩展名、多点文件名、大写扩展名
  特殊内容：空文件、GBK 编码文件
"""
import os
import shutil
import sys
import time
import traceback
import zipfile
from pathlib import Path

os.environ["GITGUI_CONFIG"] = str(Path(__file__).parent / "_cfg.json")
sys.path.insert(0, str(Path(__file__).parent))
import git_manager

base = Path(__file__).parent
repo = base / f"_类型测试_{os.getpid()}"
for _ in range(5):
    shutil.rmtree(repo, ignore_errors=True)
    if not repo.exists():
        break
    time.sleep(0.3)
repo.mkdir(exist_ok=True)

TEXT_FILES = {
    "main.py": "print('hello')\n",
    "core.c": "int main(void){return 0;}\n",
    "core.h": "#define N 8\n",
    "cpu.v": "module cpu; endmodule\n",
    "top.sv": "module top; endmodule\n",
    "readme.txt": "第一行\n第二行\n",
    "notes.md": "# 标题\n\n内容\n",
    "config.json": '{"a": 1}\n',
    "data.xml": "<root><a/></root>\n",
    "ci.yaml": "key: value\n",
    "setup.ini": "[sec]\nk=v\n",
    "app.cfg": "debug=1\n",
    "table.csv": "a,b,c\n1,2,3\n",
    "build.tcl": "puts hello\n",
    "prog.asm": "MOV R1, R2\n",
    "rom.mif": "DEPTH = 16;\n",
    "proj.qsf": "set_global_assignment -name FAMILY Cyclone\n",
    "run.sh": "echo hi\n",
    "build.bat": "echo off\n",
    "run.log": "line1\nline2\n",
    "扩展名大写.TXT": "大写扩展名\n",
    "无扩展名文件": "没有扩展名\n",
    "多点.名字.v1.txt": "多个点\n",
    "带 空格 的文件.txt": "空格文件名\n",
    "特殊#&%+()[]符号.txt": "特殊字符\n",
    "sub/dir/深层文件.v": "module deep; endmodule\n",
}
for name, content in TEXT_FILES.items():
    p = repo / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")

# GBK 编码文本文件（中文 Windows 项目常见）
(repo / "gbk编码.txt").write_bytes("中文内容第一行\n第二行\n".encode("gbk"))
# 空文件
(repo / "空文件.txt").write_text("", encoding="utf-8")

# 二进制类文件（造出真实二进制头）
(repo / "文档.docx").write_bytes(b"PK\x03\x04" + bytes(range(256)) * 4)
(repo / "表格.xlsx").write_bytes(b"PK\x03\x04" + bytes(range(256)) * 4)
(repo / "图纸.vsdx").write_bytes(b"PK\x03\x04" + bytes(range(256)) * 4)
(repo / "手册.pdf").write_bytes(b"%PDF-1.4\n" + bytes([0, 1, 2, 255]) * 100)
(repo / "图像.png").write_bytes(b"\x89PNG\r\n\x1a\n" + bytes([0, 255]) * 200)
with zipfile.ZipFile(repo / "压缩包.zip", "w") as z:
    z.writestr("inside.txt", "hello")
(repo / "程序.exe").write_bytes(b"MZ" + bytes([0, 1]) * 500)
(repo / "缓存.pyc").write_bytes(b"\x00\x00\x00\x00" + bytes(range(200)))

# 初始化仓库并把「基准版本」提交（用于制造"已修改"）
g = git_manager.Git(repo_dir=repo)
g.run("init", "-b", "main")
g.run("config", "user.name", "T")
g.run("config", "user.email", "t@t.com")
g.run("add", "-A")
g.run("commit", "-m", "基准版本")
# 建立 .gitignore 只忽略测试脚本自身产物
(repo / ".gitignore").write_text("*.tmp\n", encoding="utf-8")
g.run("add", "-A")
g.run("commit", "-m", "加入 .gitignore")

# 制造修改：文本文件全部改动，并新增未跟踪文件
for name in TEXT_FILES:
    p = repo / name
    p.write_text(p.read_text(encoding="utf-8", errors="replace") + "修改过的内容\n",
                 encoding="utf-8")
(repo / "新文件.txt").write_text("新增的文本文件\n", encoding="utf-8")
(repo / "新图片.png").write_bytes(b"\x89PNG\r\n\x1a\n" + bytes([0, 255]) * 50)
(repo / "新文档.docx").write_bytes(b"PK\x03\x04" + bytes(range(256)) * 3)
# 二进制文件也改动一下
(repo / "图像.png").write_bytes(b"\x89PNG\r\n\x1a\n" + bytes([0, 255]) * 300)
(repo / "手册.pdf").write_bytes(b"%PDF-1.4\n" + bytes([0, 1, 2, 255]) * 120)
# GBK 编码文件：修改（已跟踪）与新增（未跟踪）都要覆盖
(repo / "gbk编码.txt").write_bytes("中文内容第一行\n第二行改动\n".encode("gbk"))
(repo / "新gbk文件.txt").write_bytes("新增GBK文件内容\n第二行\n".encode("gbk"))
# 空文件 -> 添加内容（覆盖"从空到有"的差异）
(repo / "空文件.txt").write_text("现在有内容了\n", encoding="utf-8")

print("仓库准备完成:", repo)
print("git status 条目数:", len(g.status()))

# ---------------- 用软件逐个检查 ----------------
BINARY_EXTS = (".docx", ".xlsx", ".vsdx", ".pdf", ".png", ".zip", ".exe", ".pyc")
app = git_manager.App()
app.withdraw()
app.addr_var.set(str(repo))
app.open_repo(repo)
deadline = time.time() + 15
while time.time() < deadline and app.busy:
    app.update()
    time.sleep(0.05)
app.update()

rows = [(app.status_tree.item(i, "values")[0],
         app.status_tree.item(i, "values")[1]) for i in app.status_tree.get_children()]
print("「变更」页签条目数:", len(rows))

results = []
for label, path in rows:
    idx = None
    for i in app.status_tree.get_children():
        if app.status_tree.item(i, "values")[1] == path:
            idx = i
            break
    if idx is None:
        results.append((path, False, "变更列表里找不到"))
        continue
    app.status_tree.selection_set(idx)
    try:
        app.on_diff()
        end = time.time() + 10
        while time.time() < end and app.busy:
            app.update()
            time.sleep(0.05)
        app.update()
    except Exception as e:  # noqa: BLE001
        results.append((path, False, f"抛异常 {e!r}"))
        traceback.print_exc()
        continue
    text = app.diff_text.get("1.0", "end").strip()
    is_bin_ext = path.lower().endswith(BINARY_EXTS)
    got_bin_note = "这是二进制文件" in text      # 提示可能在首行之后（未跟踪文件带前缀）
    expect_bin = is_bin_ext
    # 判定：错误信息 / 空内容 / 分类不符 都算失败
    ok = True
    note = ""
    if not text:
        ok, note = False, "差异页签为空"
    elif "fatal:" in text or "error:" in text:
        ok, note = False, f"git 报错: {text.splitlines()[0][:60]}"
    elif got_bin_note != expect_bin:
        ok, note = False, f"二进制判定不符: 提示={got_bin_note} 期望={expect_bin}"
    else:
        note = "二进制提示" if got_bin_note else "逐行差异"
    # GBK 文件检查乱码（已跟踪与未跟踪两种情况）
    if path.endswith("gbk编码.txt") and not got_bin_note:
        if "中文内容第一行" not in text:
            ok, note = False, "GBK 已跟踪文件乱码（未正确解码）"
    if path == "新gbk文件.txt" and not got_bin_note:
        if "新增GBK文件内容" not in text:
            ok, note = False, "GBK 未跟踪文件预览乱码（未正确解码）"
    if path == "空文件.txt" and not got_bin_note:
        if "现在有内容了" not in text:
            ok, note = False, "空文件差异异常"
    results.append((path, ok, note))

app.destroy()
os.remove(base / "_cfg.json")
print("\n" + "=" * 70)
fails = 0
for path, ok, note in results:
    if not ok:
        fails += 1
    print(f"[{'PASS' if ok else 'FAIL'}] {path}  —— {note}")
print("=" * 70)
print(f"共 {len(results)} 项，失败 {fails} 项")
# 清理
for _ in range(6):
    shutil.rmtree(repo, ignore_errors=True)
    if not repo.exists():
        break
    time.sleep(0.4)
sys.exit(1 if fails else 0)
