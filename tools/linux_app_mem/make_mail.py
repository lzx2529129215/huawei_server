#!/usr/bin/env python3
"""Write a local test mailbox (mbox) for the mail clients: 10,000 messages, made-up senders.

  - plain-text and HTML mails with varied subjects and bodies (Chinese and English)
  - every 50th mail carries a JPEG attachment (about 1-2 MB, cut from photo.jpg of the GIMP set)
  - every 500th mail carries a PDF attachment (about 1 MB)
  - the newest 50 are unread
Output: materials/mail/Inbox (mbox) and the same messages as a Maildir (materials/mail/maildir).
"""
import email.utils
import mailbox
import random
import subprocess
import time
from email.message import EmailMessage
from pathlib import Path

OUT = Path.home() / "linux_app_mem/materials/mail"
N = 10000
random.seed(7)
WORDS = ("项目 进度 会议 周报 预算 采购 合同 测试 版本 发布 客户 反馈 方案 评审 需求 安排 设计 数据 报表 系统 "
         "report meeting schedule budget review release draft update invoice travel plan agenda notes data").split()


def jpegs():
    src = Path.home() / "linux_app_mem/materials/gimp/photo.jpg"
    out = []
    for i, (w, h) in enumerate([(1600, 1200), (2000, 1500), (2400, 1800)]):
        p = OUT / f"att_{i}.jpg"
        subprocess.run(["convert", str(src), "-crop", f"{w}x{h}+{i * 200}+{i * 150}", "+repage", "-quality", "88", str(p)], check=True)
        out.append(p.read_bytes())
    return out


def pdf():
    for p in (Path.home() / "linux_app_mem/materials/librecad/large.pdf", OUT / "att.pdf"):
        if p.exists():
            return p.read_bytes()
    p = OUT / "att.pdf"
    subprocess.run(["convert", str(Path.home() / "linux_app_mem/materials/gimp/photo.jpg"), "-resize", "50%", str(p)], check=True)
    return p.read_bytes()


def body(k):
    lines = []
    for _ in range(random.randint(5, 40)):
        lines.append(" ".join(random.choice(WORDS) for _ in range(random.randint(6, 18))))
    return "\n".join(lines) + f"\n\n-- \n测试邮件 #{k}\n"


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    imgs, pdfb = jpegs(), pdf()
    mbox_path = OUT / "Inbox"
    mbox_path.unlink(missing_ok=True)
    mb = mailbox.mbox(str(mbox_path))
    md = mailbox.Maildir(str(OUT / "maildir"), create=True)
    md.clear()
    t0 = time.time() - N * 1800
    for k in range(N):
        m = EmailMessage()
        m["From"] = f"Sender {k % 97} <sender{k % 97}@example.com>"
        m["To"] = "tester@example.com"
        m["Subject"] = f"[{random.choice(WORDS)}] {' '.join(random.choice(WORDS) for _ in range(4))} #{k}"
        m["Date"] = email.utils.formatdate(t0 + k * 1800, localtime=True)
        m["Message-ID"] = f"<test-{k}@example.com>"
        text = body(k)
        if k % 3 == 0:
            m.set_content(text)
            m.add_alternative("<html><body>" + "".join(f"<p>{l}</p>" for l in text.splitlines()) + "</body></html>", subtype="html")
        else:
            m.set_content(text)
        if k % 50 == 0:
            m.add_attachment(imgs[k // 50 % 3], maintype="image", subtype="jpeg", filename=f"photo_{k}.jpg")
        if k % 500 == 0:
            m.add_attachment(pdfb, maintype="application", subtype="pdf", filename=f"plan_{k}.pdf")
        mm = mailbox.mboxMessage(m)
        if k < N - 50:
            mm.set_flags("RO")
        mb.add(mm)
        dm = mailbox.MaildirMessage(m)
        if k < N - 50:
            dm.set_subdir("cur"); dm.set_flags("S")
        md.add(dm)
    mb.flush(); mb.close()
    print(mbox_path, mbox_path.stat().st_size // (1 << 20), "MiB,", N, "messages")


if __name__ == "__main__":
    main()
