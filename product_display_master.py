import re
from functools import lru_cache
from product_display_data import DATA

@lru_cache(maxsize=1)
def exact_master():
    """User-approved item-code master, 2026-09-21 (1262 codes)."""
    return DATA

CATNUM={'01':'국내','02':'일반수출','03':'스트라우만','04':'포인트닉스','05':'디오','06':'NJ메디','07':'덴티스','08':'하이니스','10':'후원EDI','11':'선광덴탈'}
SIZE_G={'015':'0.15g','025':'0.25g','050':'0.5g','100':'1.0g','200':'2.0g','300':'3.0g'}
SIZE_CC={'025':'0.25cc','030':'0.3cc','050':'0.5cc','060':'0.6cc','100':'1.0cc','150':'1.5cc','200':'2.0cc','250':'2.5cc','300':'3.0cc','400':'4.0cc','500':'5.0cc'}
ALLO_SIZE={'015H':'0.15g','030':'0.3cc','050':'0.5cc','060':'0.6cc','100':'1.0cc','200':'2.0cc','300':'3cc','400':'4cc','500':'5.0cc','600':'6cc','1000':'10cc','1200':'12.0cc','2000':'20.0cc'}
TENDON={'41A':'Achilles Tendon','41G':'Gracilis Tendon','41P':'Peroneus Longus Tendon','41Q':'Quadriceps Tendon','41S':'Semitendinosus Tendon','41T':'Tibialis Tendon'}

def cat(c):
    u=c.upper()
    if '-14-FDA' in u:return 'Zimvie(FDA)'
    if '-14-CE' in u:return 'Zimvie(CE)'
    if u.endswith('-02-EGY'):return '이집트'
    if u.endswith('-02-TW'):return '대만'
    if u.endswith('-02-FDA'):return 'FDA'
    if u.endswith('-02-CE') or u.endswith('-CE'):return 'CE'
    if u.startswith('34A') and u.endswith('-12'):return '아이비덴탈'
    if u.startswith(('39F','39H')) and u.endswith('-12'):return '하하겐'
    if u.startswith(('33A','34A')) and u.endswith('-13'):return '메디피아'
    if u.startswith('48A') and u.endswith('-13'):return '사우디'
    if u.endswith('-PO') or (u.endswith('H-02') and u.startswith(('11B','48A','49A'))):return '핸즈온'
    tail=u.split('-')[-1]
    if u.startswith('16B') and ('S-' in u or u in {'16BP015-01','16BP100-01'}):return '샘플'
    if tail in CATNUM:return CATNUM[tail]
    if tail in {'OB','URO'}:return '국내'
    if u.startswith('42A') and tail=='12':return '일반수출'
    return '확인 필요'

def dim_from_digits(d):
    if len(d)==4:
        a,b=d[:2],d[2:]
        return f'{int(a)}×{int(b)}㎠'
    if len(d)==6:
        a,b=d[:3],d[3:]
        af=int(a)/10
        bf=float(int(b)) if int(b)<10 else int(b)/10
        def f(x): return str(int(x)) if x.is_integer() else str(x)
        return f'{f(af)}×{f(bf)}㎠'
    return '-'

def _classify(c):
    u=c.upper()
    p=u[:3]
    if p in TENDON:
        return {'name':'Tendon','type':TENDON[p],'size':'-','category':'국내'}
    if p in {'11B','12B','14B'}:
        m=re.match(r'^(..B)([CP])(\d{3})',u)
        typ='Chip' if m.group(2)=='C' else 'Powder'
        size=SIZE_G[m.group(3)]
        if p=='11B':
            name='BOSS' if ('-02-CE' in u or '-02-EGY' in u or '-14-' in u) else 'BONE-XB'
        elif p=='12B':
            name='S1' if ('-02-CE' in u or '-02-FDA' in u or '-02-TW' in u or '-14-' in u) else 'S1-XB'
        else:
            name='Bone-XB+'
        return {'name':name,'type':typ,'size':size,'category':cat(u)}
    if p in {'21P','24P'}:
        m=re.match(r'^..P([CP])(\d{3})',u)
        typ='Chip' if m.group(1)=='C' else 'Powder'
        return {'name':'Bone-XP' if p=='21P' else 'Bone-XP+','type':typ,'size':SIZE_G[m.group(2)],'category':cat(u)}
    if p=='16B':
        raw=re.match(r'^16BP(\d+)',u).group(1)
        sm={'015':'0.15g','0300':'3.0g','100':'1.0g','1000':'10.0g'}[raw]
        return {'name':'Medpark Medical','type':'Powder','size':sm,'category':cat(u)}
    if p in {'33A','34A','35A'}:
        raw=re.match(r'^\d\dAS(\d{3})',u).group(1)
        names={'33A':'A1 OSS 5','34A':'A1 OSS 7','35A':'A1 OSS 10'}
        return {'name':names[p],'type':'Syringe','size':SIZE_CC[raw],'category':cat(u)}
    if p=='36A':
        raw=re.match(r'^36AT(\d{3})',u).group(1)
        return {'name':'A1 Bone Chip','type':'Tyvek','size':f'{int(raw)/10:.1f}cc','category':cat(u)}
    if p=='37A':
        raw=re.match(r'^37AS(\d{3})',u).group(1)
        return {'name':'A1 DBM','type':'Syringe','size':SIZE_CC[raw],'category':cat(u)}
    if p in {'38C','38F','38H'}:
        if '-URO' in u:return {'name':'S Derm(FD) Urology','type':'5mm','size':'6cm','category':'국내'}
        if '-OB' in u:return {'name':'S Derm(FD) Ob-Gyn','type':'8mm','size':'6cm','category':'국내'}
        body=u.split('-')[0][4:]
        thick=body[:2]
        dims=body[2:]
        tmap={'01':'0~1mm','12':'1~2mm','23':'2~3mm','34':'3~4mm','45':'4~5mm','56':'5~6mm','67':'6~7mm','78':'7~8mm'}
        name={'38C':'S Derm(CP)','38F':'S Derm(FD)','38H':'S Derm(HD)'}[p]
        size=dim_from_digits(dims)
        if u=='38CP341616-01':size='6×16㎠'
        return {'name':name,'type':tmap[thick],'size':size,'category':cat(u)}
    if p in {'40C','40F'}:
        body=u.split('-')[0][4:]
        return {'name':'S Derm(CP)' if p=='40C' else 'S Derm(FD)','type':'0.3~0.6mm','size':dim_from_digits(body[4:]),'category':cat(u)}
    if p in {'39F','39H'}:
        raw=re.match(r'^39[HF]D(\d{3})',u).group(1)
        if p=='39F':
            name='HAHA GEN' if u.endswith('-12') else ('S Gen (Ob-Gyn)' if '-OB' in u else 'S Gen')
        else:
            name='HAHA GEN INJECT' if u.endswith('-12') else 'S Gen Inject'
        return {'name':name,'type':'Syringe','size':SIZE_CC[raw],'category':cat(u)}
    if p=='42A':
        raw=re.match(r'^42AP([^\-]+)',u).group(1)
        return {'name':'S1-Allo 덴탈','type':'Powder','size':ALLO_SIZE[raw],'category':cat(u)}
    if p=='43C':
        return {'name':'Costal Cartilage','type':'-','size':'-','category':'국내'}
    if p=='44A':
        m=re.match(r'^44A([CP])([^\-]+)',u)
        size=ALLO_SIZE[m.group(2)]
        if u=='44AP500-01':size='10cc'
        return {'name':'S1-Allo 메디컬','type':'Chip' if m.group(1)=='C' else 'Powder','size':size,'category':'국내'}
    if p in {'45F','45H'}:
        raw=re.match(r'^45[HF]D(\d{3})',u).group(1)
        return {'name':'MedPark Fill','type':'Syringe','size':SIZE_CC[raw],'category':cat(u)}
    if p in {'46F','47F'}:
        return {'name':'Adite','type':'70㎛ 이하' if p=='46F' else '100㎛ 이하','size':'150mg','category':cat(u)}
    if p in {'48A','49A'}:
        raw=re.match(r'^4[89]AP([^\-]+)',u).group(1)
        return {'name':'MedParkAlloD','type':'Powder','size':ALLO_SIZE[raw],'category':cat(u)}
    if p in {'5BH','5BS','5DM'}:
        body=u.split('-')[0][3:]
        name='Colla' if any(x in u for x in ('-CE','-EGY','-TW')) else 'Colla-DM'
        return {'name':name,'type':{'5BH':'Hard','5BS':'Soft','5DM':'DM'}[p],'size':f'{int(body[:2])}×{int(body[2:])}mm','category':cat(u)}
    return None

def runtime_master(refs):
    if not isinstance(refs, dict):
        return None
    payload = refs.get('product_display', {}).get('master', {})
    items = payload.get('items') if isinstance(payload, dict) else None
    return items if isinstance(items, dict) and items else None

def lookup(icube, fallback_name="", fallback_spec="", refs=None):
    code=str(icube or "").strip().upper()
    master=runtime_master(refs)
    item=(master if master is not None else exact_master()).get(code)
    try:
        item=item or _classify(code)
    except Exception:
        item=None
    if not item:
        return {'name':fallback_name or '제품명 미등록','type':'마스터 미등록','size':fallback_spec or '-','category':'확인 필요','mapped':False,'icube':code}
    result=dict(item)
    result.update(mapped=True,icube=code)
    return result
