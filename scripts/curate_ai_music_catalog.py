"""Stage full original AI tracks from advertised public downloads; never edit live media."""
from __future__ import annotations
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sys
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen
from bs4 import BeautifulSoup
from mutagen.mp3 import MP3
from mutagen.id3 import TIT2,TPE1,TALB,TCON,TDRC,TXXX,TCOP,WOAS,WCOP

GENRES={'pop':'Pop','hip-hop-rap':'Hip-Hop','dance-electronic':'Electronic','rock':'Rock','jazz':'Jazz','acoustic-folk':'Folk','lofi-chill':'Lo-Fi'}
BASE='https://starsinger.ai'
LICENSE=BASE+'/free-music-for-youtube'

def utc():return datetime.now(timezone.utc).isoformat()
def write(path,value):
    tmp=path.with_suffix(path.suffix+'.part')
    tmp.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8');tmp.replace(path)
def page(url):
    if urlparse(url).hostname!='starsinger.ai':raise ValueError('outside public catalog origin')
    with urlopen(Request(url,headers={'User-Agent':'RadioTEDU-AI-Catalog-Curation/1.0 (radiotedu.com)'}),timeout=20) as r:data=r.read(2_000_000)
    return BeautifulSoup(data,'html.parser'),hashlib.sha256(data).hexdigest(),data
def links(soup,kinds):
    urls=[urljoin(BASE,a['href']).split('?')[0].split('#')[0] for a in soup.find_all('a',href=True)]
    return list(dict.fromkeys(u for u in urls if urlparse(u).hostname=='starsinger.ai' and any(urlparse(u).path.startswith('/'+k+'/') for k in kinds)))
def recording(soup):
    for node in soup.find_all('script',type='application/ld+json'):
        try:data=json.loads(node.string or node.get_text())
        except ValueError:continue
        for item in data.get('@graph',[data]) if isinstance(data,dict) else data:
            if isinstance(item,dict) and item.get('@type')=='MusicRecording':return item
    raise ValueError('public MusicRecording missing')
def seconds(value):
    m=re.fullmatch(r'PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?',str(value))
    return sum(float(v or 0)*f for v,f in zip(m.groups(),[3600,60,1])) if m else 0
def safe(value):return re.sub(r'[<>:"/\\|?*\x00-\x1f]','',value).strip(' .')[:130]

def artist(url,genre):
    soup,digest,_=page(url);text=soup.get_text(' ',strip=True)
    if 'AI-generated' not in text and 'AI ' not in text:raise ValueError('AI disclosure missing')
    if any(t in text.casefold() for t in ['istanbul, turkey','ankara, turkey','izmir, turkey','turkish singer','azerbaijani singer']):raise ValueError('Turkish or related repertoire')
    return {'url':url,'genre':genre,'source_page_sha256':digest,'songs':links(soup,['song','video']),'similar':links(soup,['artist'])}

def song(url,fallback,detect):
    soup,digest,_=page(url);x=recording(soup)
    name=x.get('name');performer=x.get('byArtist',{}).get('name');audio=x.get('audio',{}).get('contentUrl');length=seconds(x.get('duration'))
    if not name or not performer or not audio or not 100<=length<=600:raise ValueError('not a complete identified radio song')
    if urlparse(audio).hostname!='media.starsinger.ai' or not urlparse(audio).path.endswith('.mp3'):raise ValueError('not advertised public MP3')
    if re.search(r'\b(test|demo|happy birthday|birthday song|karaoke|instrumental loop)\b',name,re.I):raise ValueError('demo, loop or personal greeting')
    lyrics=x.get('lyrics',{}).get('text','');language=detect(lyrics) if len(lyrics)>80 else x.get('inLanguage')
    if language in ['tr','az'] or re.search(r'[ğĞıİşŞ]',lyrics+name):raise ValueError('Turkish/Azerbaijani evidence')
    if re.search(r'[\u0400-\u052f\u0600-\u06ff\u0900-\u0dff\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]',name):raise ValueError('original title unsupported by current EN/FR speech pronunciation')
    plays=None
    for counter in x.get('interactionStatistic',[]):
        if 'ListenAction' in str(counter.get('interactionType')):plays=counter.get('userInteractionCount')
    return {'source_id':urlparse(x.get('url') or url).path.rstrip('/').split('/')[-1],'title':name,'artist':performer,
        'genre':GENRES.get(x.get('genre'),fallback),'source_genre':x.get('genre'),'source_url':x.get('url') or url,'audio_url':audio,
        'source_duration_seconds':length,'language':language,'source_declared_language':x.get('inLanguage'),'source_play_count':plays,
        'source_published_at':x.get('datePublished'),'ai_provenance':'Star Singer original AI catalog; publisher AI disclosure',
        'license':'Star Singer public commercial-use grant; copyright retained by publisher','license_url':LICENSE,
        'source_page_sha256':digest,'observed_at':utc()}

def download(row,staging):
    target=staging/row['genre']/(safe(row['artist']+' - '+row['title'])+' ['+row['source_id']+'].mp3')
    target.parent.mkdir(parents=True,exist_ok=True);tmp=target.with_suffix('.mp3.part')
    if not target.exists():
        with urlopen(Request(row['audio_url'],headers={'User-Agent':'RadioTEDU-AI-Catalog-Curation/1.0'}),timeout=35) as r,tmp.open('wb') as out:
            while chunk:=r.read(256*1024):out.write(chunk)
        a=MP3(tmp)
        if not 95<=a.info.length<=610 or a.info.bitrate<96000:raise ValueError('short or low-bitrate download')
        if abs(a.info.length-row['source_duration_seconds'])>max(10,row['source_duration_seconds']*.1):raise ValueError('length disagrees with published track')
        if a.tags is None:a.add_tags()
        a.tags.add(TIT2(encoding=3,text=row['title']));a.tags.add(TPE1(encoding=3,text=row['artist']))
        a.tags.add(TALB(encoding=3,text='Star Singer AI Originals'));a.tags.add(TCON(encoding=3,text=row['genre']))
        if row.get('source_published_at'):a.tags.add(TDRC(encoding=3,text=row['source_published_at'][:10]))
        a.tags.add(TCOP(encoding=3,text='Star Singer / Veronata, Inc. — commercial-use grant per source'))
        a.tags.add(WOAS(url=row['source_url']));a.tags.add(WCOP(url=LICENSE))
        for key,value in [('AI_GENERATED','true'),('SOURCE_ID',row['source_id']),('SOURCE_LANGUAGE',row.get('language') or 'und')]:a.tags.add(TXXX(encoding=3,desc=key,text=value))
        a.tags.save(tmp);tmp.replace(target)
    a=MP3(target)
    return {**row,'staged_file':str(target),'duration_seconds':a.info.length,'bitrate_bps':a.info.bitrate,'bytes':target.stat().st_size,'sha256':hashlib.sha256(target.read_bytes()).hexdigest()}

def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--work',type=Path,required=True);ap.add_argument('--staging',type=Path,required=True)
    ap.add_argument('--language-deps',type=Path,required=True);ap.add_argument('--target',type=int,default=1000)
    ap.add_argument('--artists-per-genre',type=int,default=45);args=ap.parse_args()
    if 'YouTube Stream' in args.staging.resolve().parts or args.staging.resolve().name=='AI':raise ValueError('use separate staging, not a live archive')
    sys.path.insert(0,str(args.language_deps));from langdetect import detect,DetectorFactory
    DetectorFactory.seed=0
    try:
        import os,psutil
        if os.name=='nt':psutil.Process().nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
    except ImportError:pass
    args.work.mkdir(parents=True,exist_ok=True);args.staging.mkdir(parents=True,exist_ok=True)
    _,license_hash,raw=page(LICENSE);(args.work/'starsinger-commercial-grant.html').write_bytes(raw)
    artist_file=args.work/'ai-curation-artists.json';song_file=args.work/'ai-curation-songs.json';rejects=[]
    if not artist_file.exists():
        pending=[]
        for key,genre in GENRES.items():
            soup,_,_=page(BASE+'/genre/'+key);pending.extend((u,genre) for u in links(soup,['artist']))
        rows=[];seen=set();counts={g:0 for g in GENRES.values()}
        while pending:
            batch=[]
            while pending and len(batch)<12:
                url,genre=pending.pop(0)
                if url in seen or counts[genre]>=args.artists_per_genre:continue
                seen.add(url);counts[genre]+=1;batch.append((url,genre))
            if not batch:continue
            with ThreadPoolExecutor(max_workers=3) as pool:
                tasks={pool.submit(artist,u,g):(u,g) for u,g in batch}
                for future in as_completed(tasks):
                    u,g=tasks[future]
                    try:r=future.result();rows.append(r);pending.extend((v,g) for v in r['similar'])
                    except Exception as exc:rejects.append({'source_url':u,'reason':type(exc).__name__+': '+str(exc)[:180]})
            write(args.work/'ai-curation-artists-progress.json',rows)
            print(json.dumps({'phase':'artists','collected':len(rows),'genre_counts':counts}),flush=True)
        write(artist_file,rows)
    artists=json.loads(artist_file.read_text(encoding='utf-8'))
    if not song_file.exists():
        by_url={u:r['genre'] for r in artists for u in r['songs']};rows=[]
        with ThreadPoolExecutor(max_workers=3) as pool:
            tasks={pool.submit(song,u,g,detect):u for u,g in by_url.items()}
            done=0
            for future in as_completed(tasks):
                try:rows.append(future.result())
                except Exception as exc:rejects.append({'source_url':tasks[future],'reason':type(exc).__name__+': '+str(exc)[:180]})
                done+=1
                if done%40==0:
                    write(args.work/'ai-curation-songs-progress.json',rows);write(args.work/'ai-curation-rejected.json',rejects)
                    print(json.dumps({'phase':'metadata','processed':done,'eligible':len(rows),'rejected':len(rejects)}),flush=True)
        write(song_file,rows)
    songs=json.loads(song_file.read_text(encoding='utf-8'));groups={g:[] for g in GENRES.values()}
    for row in songs:groups[row['genre']].append(row)
    for group in groups.values():group.sort(key=lambda r:(-(r.get('source_play_count') or 0),abs(r['source_duration_seconds']-210),r['source_id']))
    ordered=[];seen=set()
    while any(groups.values()) and len(ordered)<args.target:
        for group in groups.values():
            while group:
                row=group.pop(0)
                if row['audio_url'] in seen:continue
                seen.add(row['audio_url']);ordered.append(row);break
            if len(ordered)>=args.target:break
    imported=[]
    with ThreadPoolExecutor(max_workers=3) as pool:
        tasks={pool.submit(download,r,args.staging):r for r in ordered}
        for future in as_completed(tasks):
            try:imported.append(future.result())
            except Exception as exc:rejects.append({'source_url':tasks[future]['source_url'],'reason':type(exc).__name__+': '+str(exc)[:180]})
            if len(imported)%15==0:
                write(args.work/'ai-curation-imported.json',imported)
                print(json.dumps({'phase':'download','complete':len(imported),'target':len(ordered)}),flush=True)
    write(args.work/'ai-curation-imported.json',imported);write(args.work/'ai-curation-rejected.json',rejects)
    print(json.dumps({'phase':'complete','imported':len(imported),'eligible_metadata':len(songs),'rejected':len(rejects),'license_sha256':license_hash,'completed_at':utc()}),flush=True)
if __name__=='__main__':main()
