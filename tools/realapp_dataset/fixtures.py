"""Deterministic, non-private documents/media for real application operations."""
from __future__ import annotations

import csv
import io
import math
import struct
import wave
from pathlib import Path

from .core import digest, save


def chunk(tag, data):
    return tag + struct.pack("<I",len(data)) + data + (b"\0" if len(data)%2 else b"")


def video(path, width, height, frames=90, fps=15):
    from PIL import Image, ImageDraw
    payload=[]
    for frame in range(frames):
        im=Image.new("RGB",(width,height),(28,38,54));draw=ImageDraw.Draw(im)
        for i in range(24):
            x=(frame*width//frames+i*width//24)%width
            y=height//2+int(math.sin((frame+i)/12)*height*.28)
            r=height//24
            draw.ellipse((x-r,y-r,x+r,y+r),fill=((i*37)%255,180,(frame*3)%255))
        draw.text((width//12,height//12),f"REALAPP TEST CLIP - FRAME {frame:03d}",fill="white")
        target=io.BytesIO();im.save(target,"JPEG",quality=85);payload.append(target.getvalue());im.close()
    largest=max(map(len,payload))
    avih=struct.pack("<14I",int(1e6/fps),largest*fps,0,0x10,frames,0,1,largest,width,height,0,0,0,0)
    strh=struct.pack("<4s4sIHH8I4h",b"vids",b"MJPG",0,0,0,0,1,fps,0,frames,largest,0xffffffff,0,0,0,width,height)
    strf=struct.pack("<IiiHH4sIiiII",40,width,height,1,24,b"MJPG",width*height*3,0,0,0,0)
    hdrl=chunk(b"LIST",b"hdrl"+chunk(b"avih",avih)+chunk(b"LIST",b"strl"+chunk(b"strh",strh)+chunk(b"strf",strf)))
    movi=bytearray(b"movi");index=bytearray()
    for data in payload:
        offset=len(movi);movi.extend(chunk(b"00dc",data));index.extend(struct.pack("<4sIII",b"00dc",0x10,offset,len(data)))
    body=b"AVI "+hdrl+chunk(b"LIST",bytes(movi))+chunk(b"idx1",bytes(index))
    path.write_bytes(b"RIFF"+struct.pack("<I",len(body))+body)


def generate(out: Path):
    from PIL import Image, ImageDraw
    out.mkdir(parents=True,exist_ok=True)
    manifest={"schema":"realapp-fixtures-v1","private_data":False,"levels":{},"calibrated":False}
    for level,scale in (("low",1),("medium",2),("high",3)):
        folder=out/level;folder.mkdir(exist_ok=True)
        for index in range(3):
            w,h=2048*scale,1536*scale
            im=Image.new("RGB",(w,h),(238,242,248));draw=ImageDraw.Draw(im)
            for i in range(200):
                x=(i*619+index*37)%w;y=(i*887)%h
                draw.rectangle((x,y,min(w-1,x+120*scale),min(h-1,y+70*scale)),fill=((i*13)%255,(i*23)%255,(i*41)%255))
            draw.text((50,50),f"REALAPP IMAGE {level} {index}",fill="black")
            im.save(folder/f"image_{index}.bmp");im.close()
        with (folder/"report.rtf").open("w",encoding="ascii") as f:
            f.write(r"{\rtf1\ansi\deff0 {\fonttbl {\f0 Arial;}}\f0\fs24 "+"\n")
            for page in range(80*scale):
                f.write(f"REALAPP TEST REPORT - PAGE {page+1}\\par\n")
                for row in range(30):f.write(f"Independent sample {page*30+row}: forecast, application switch and memory measurement.\\par\n")
                f.write("\\page\n")
            f.write("}")
        with (folder/"table.csv").open("w",encoding="utf-8",newline="") as f:
            writer=csv.writer(f);writer.writerow(["id","quantity","price","subtotal"])
            for i in range(1,10000*scale+1):writer.writerow([i,i%97+1,round((i%71)/3,2),f"=B{i+1}*C{i+1}"])
        with (folder/"drawing.dxf").open("w",encoding="ascii") as f:
            f.write("0\nSECTION\n2\nENTITIES\n")
            for i in range(3000*scale):
                x,y=i%200,i//200
                f.write(f"0\nLINE\n8\nLayer{i%5}\n10\n{x}\n20\n{y}\n30\n0\n11\n{x+.8}\n21\n{y+.7}\n31\n0\n")
            f.write("0\nENDSEC\n0\nEOF\n")
        (folder/"outline.txt").write_text("Test project\n"+"".join(f"\tTopic {i}\n\t\tTask A\n\t\tTask B\n" for i in range(100*scale)),encoding="utf-8")
        (folder/"diagram.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg" width="1600" height="1200">'+''.join(f'<rect x="{i%20*80}" y="{i//20*50}" width="60" height="35" fill="#69a7db"/><text x="{i%20*80+4}" y="{i//20*50+22}" font-size="10">Node {i}</text>' for i in range(400))+'</svg>',encoding="utf-8")
        with wave.open(str(folder/"audio.wav"),"wb") as f:
            f.setnchannels(1);f.setsampwidth(2);f.setframerate(44100)
            f.writeframes(b"".join(struct.pack("<h",int(3000*math.sin(2*math.pi*440*i/44100))) for i in range(44100*6)))
        video(folder/"video.avi",640*scale,360*scale)
        (folder/"reference.html").write_text('<!doctype html><meta charset="utf-8"><title>Real application test reference</title><h1>Independent test reference</h1>'+''.join(f'<h2>Section {i}</h2><p>Fixed test material for reading, scrolling and revisiting.</p>' for i in range(500*scale))+'<img src="image_0.bmp" width="1000">',encoding="utf-8")
        manifest["levels"][level]=[{"path":p.relative_to(out).as_posix(),"sha256":digest(p),"bytes":p.stat().st_size} for p in sorted(folder.iterdir())]
        print("fixtures: "+level,flush=True)
    save(out/"manifest.json",manifest)
    return manifest
