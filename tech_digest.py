#!/usr/bin/env python3
"""Generate a Chinese daily tech digest, mobile poster, and PushPlus delivery."""
from __future__ import annotations

import argparse, html, os, re, sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urljoin
from xml.etree import ElementTree
import requests
from bs4 import BeautifulSoup
from PIL import Image, ImageDraw, ImageFont

CN = timezone(timedelta(hours=8)); OUT = Path("output"); POSTER = OUT / "tech-digest.png"
FEEDS = {
 "AI / 大模型 / AI 编程":{"OpenAI":"https://openai.com/news/rss.xml","Google AI":"https://blog.google/technology/ai/rss/","Hugging Face":"https://huggingface.co/blog/feed.xml","GitHub":"https://github.blog/ai-and-ml/feed/"},
 "PC 硬件":{"Tom's Hardware":"https://www.tomshardware.com/feeds.xml","TechPowerUp":"https://www.techpowerup.com/rss/news"},
 "Steam / 游戏 / 主机":{"Steam News":"https://store.steampowered.com/feeds/news.xml","PlayStation":"https://blog.playstation.com/feed/","Nintendo Life":"https://www.nintendolife.com/feeds/latest"},
 "科技 / 数码新品":{"The Verge":"https://www.theverge.com/rss/index.xml","Engadget":"https://www.engadget.com/rss.xml","Ars Technica":"https://feeds.arstechnica.com/arstechnica/index"},
}
WORDS={"AI / 大模型 / AI 编程":("ai","model","llm","agent","codex","coding","openai","gemini"),"PC 硬件":("gpu","cpu","graphics","geforce","radeon","ryzen","intel","memory","ssd"),"Steam / 游戏 / 主机":("steam","game","gaming","playstation","xbox","nintendo","switch","console"),"科技 / 数码新品":("launch","announces","phone","laptop","tablet","wearable","camera","device","chip")}

@dataclass
class Item:
 category:str; source:str; title:str; summary:str; url:str; published:datetime; score:float=0

def field(node,names):
 for child in list(node):
  if child.tag.rsplit("}",1)[-1].lower() in names:
   return (child.text or child.attrib.get("href","")).strip()
 return ""

def clean(value,limit=500):
 return re.sub(r"\s+"," ",html.unescape(BeautifulSoup(value or "","html.parser").get_text(" ",strip=True)))[:limit]

def date(value):
 try:
  d=parsedate_to_datetime(value)
  return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).astimezone(CN)
 except (TypeError,ValueError,OverflowError):
  try:return datetime.fromisoformat(value.replace("Z","+00:00")).astimezone(CN)
  except (TypeError,ValueError):return datetime.now(CN)

def translate(value,session):
 if not value or re.search(r"[\u4e00-\u9fff]",value):return value
 try:
  r=session.get("https://translate.googleapis.com/translate_a/single",params={"client":"gtx","sl":"auto","tl":"zh-CN","dt":"t","q":value[:1200]},timeout=20);r.raise_for_status()
  return "".join(x[0] for x in r.json()[0] if x and x[0]).strip() or value
 except (requests.RequestException,ValueError,IndexError,TypeError):return value

def collect(session):
 cutoff=datetime.now(CN)-timedelta(hours=72); found=[]; warnings=[]
 for category,feeds in FEEDS.items():
  for source,url in feeds.items():
   try:
    r=session.get(url,timeout=25);r.raise_for_status();root=ElementTree.fromstring(r.content)
    entries=[n for n in root.iter() if n.tag.rsplit("}",1)[-1].lower() in {"item","entry"}]
    for e in entries[:30]:
     title=clean(field(e,("title",)),240); link=field(e,("link",)); pub=date(field(e,("pubdate","published","updated","date")))
     if title and link and pub>=cutoff:found.append(Item(category,source,title,clean(field(e,("description","summary","content"))),urljoin(url,link),pub))
   except (requests.RequestException,ElementTree.ParseError) as exc:warnings.append(f"{source}: {exc.__class__.__name__}")
 seen=set(); unique=[]
 for x in sorted(found,key=lambda i:i.published,reverse=True):
  key=re.sub(r"\W+","",x.title.lower())
  if key in seen:continue
  seen.add(key); age=max(0,(datetime.now(CN)-x.published).total_seconds()/3600); text=(x.title+" "+x.summary).lower()
  x.score=max(0,72-age)+8*sum(k in text for k in WORDS[x.category]);unique.append(x)
 return unique,warnings

def choose(items):
 ranked=sorted(items,key=lambda x:x.score,reverse=True); selected=[]
 for category in FEEDS:
  x=next((i for i in ranked if i.category==category and i not in selected),None)
  if x:selected.append(x)
 selected.extend(x for x in ranked if x not in selected)
 return selected[:5]

def report(items,warnings):
 rows=[f"# 每日科技兴趣简报｜{datetime.now(CN):%Y年%m月%d日}","","聚焦 AI、PC 硬件、游戏主机与数码新品，精选今日最值得关注的 5 条。"]
 if not items:rows += ["","今天暂未获取到可用资讯，请稍后重试。"]
 for n,x in enumerate(items,1):rows += ["",f"## {n}. {x.title}",f"**{x.category} · {x.source} · {x.published:%m-%d %H:%M}**","",(x.summary or "原始资讯未提供摘要，请打开原文查看详情。")[:260],"",f"[阅读原文]({x.url})"]
 if warnings:rows += ["",f"> 部分来源暂不可用：{'；'.join(warnings[:4])}。其余来源已正常参与筛选。"]
 return "\n".join(rows)

def getfont(size,bold=False):
 paths=(["/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc","/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"] if bold else ["/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc","/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"])
 for p in paths:
  if Path(p).exists():return ImageFont.truetype(p,size)
 return ImageFont.load_default()

def wrap(draw,value,font,width):
 lines=[]; current=""
 for char in value:
  trial=current+char
  if current and draw.textlength(trial,font=font)>width:lines.append(current);current=char
  else:current=trial
 if current:lines.append(current)
 return lines

def make_poster(items):
 width,margin=1080,72; image=Image.new("RGB",(width,4000),"#F4F7FB");d=ImageDraw.Draw(image)
 title,datef,itemf,body,meta=getfont(60,1),getfont(30),getfont(38,1),getfont(29),getfont(24)
 d.rounded_rectangle((36,36,width-36,300),32,fill="#14213D");d.text((margin,82),"每日科技兴趣简报",font=title,fill="white");d.text((margin,185),datetime.now(CN).strftime("%Y年%m月%d日 · TOP 5"),font=datef,fill="#8ED1FC")
 colors={"AI / 大模型 / AI 编程":"#6C5CE7","PC 硬件":"#0984E3","Steam / 游戏 / 主机":"#00A884","科技 / 数码新品":"#E17055"};y=350
 for n,x in enumerate(items,1):
  titles=wrap(d,f"{n}. {x.title}",itemf,width-margin*2)[:3]; summaries=wrap(d,x.summary or "原始资讯未提供摘要。",body,width-margin*2)[:5];h=118+52*len(titles)+43*len(summaries)
  d.rounded_rectangle((36,y,width-36,y+h),28,fill="white");d.rounded_rectangle((margin,y+34,margin+330,y+76),18,fill=colors[x.category]);d.text((margin+18,y+39),x.category,font=meta,fill="white");ty=y+100
  for line in titles:d.text((margin,ty),line,font=itemf,fill="#152238");ty+=52
  d.text((margin,ty+5),f"{x.source} · {x.published:%m-%d %H:%M}",font=meta,fill="#718096");ty+=48
  for line in summaries:d.text((margin,ty),line,font=body,fill="#39465E");ty+=43
  y+=h+28
 d.text((margin,y+16),"GitHub Actions · 每天北京时间 09:00",font=meta,fill="#718096");y+=80;OUT.mkdir(exist_ok=True);image.crop((0,0,width,y)).save(POSTER,optimize=True);return POSTER

def send(content,path):
 token=os.getenv("PUSHPLUS_CLAWBOT_TOKEN") or os.getenv("PUSHPLUS_TOKEN")
 if not token:raise RuntimeError("未设置 PUSHPLUS_TOKEN 或 PUSHPLUS_CLAWBOT_TOKEN")
 image_url=os.getenv("PUBLIC_IMAGE_URL")
 if not image_url:raise RuntimeError("未设置 PUBLIC_IMAGE_URL")
 payload={"token":token,"title":"每日科技兴趣简报","content":f'<img src="{html.escape(image_url,quote=True)}" style="max-width:100%" /><br/><p>{html.escape(content[:500])}</p>',"template":"html","channel":"clawbot"}
 r=requests.post("https://www.pushplus.plus/send",json=payload,timeout=60);r.raise_for_status();result=r.json()
 if str(result.get("code")) not in {"0","200"}:raise RuntimeError(f"PushPlus 返回异常：{result}")

def main():
 parser=argparse.ArgumentParser();parser.add_argument("--generate-only",action="store_true");parser.add_argument("--send-existing",action="store_true");args=parser.parse_args()
 if args.send_existing:
  send((OUT/"tech-digest.md").read_text(encoding="utf-8"),POSTER);return 0
 session=requests.Session();session.headers["User-Agent"]="Mozilla/5.0 TechDigestBot/1.0";items,warnings=collect(session);items=choose(items)
 for x in items:x.title=translate(x.title,session);x.summary=translate(x.summary,session)
 content=report(items,warnings);path=make_poster(items);(OUT/"tech-digest.md").write_text(content,encoding="utf-8")
 if not args.generate_only:send(content,path)
 print(content);return 0
if __name__=="__main__":sys.exit(main())
