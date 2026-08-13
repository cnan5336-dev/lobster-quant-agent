import html
import json
import os

from .config import CHARTS_DIR, load_last_result, update_chart_path


def render_last_chart():
    result = load_last_result()
    bars = result.get("bars", [])
    if not bars:
        raise RuntimeError("最近一次回测结果没有图表所需K线数据。")
    os.makedirs(CHARTS_DIR, exist_ok=True)
    chart_path = os.path.join(
        CHARTS_DIR,
        f"{result['symbol']}_{result['interval']}_{result['result_id']}.html",
    )
    payload = {
        "bars": bars,
        "trades": result.get("trades", []),
        "symbol": result["symbol"],
        "interval": result["interval"],
        "strategy": result.get("strategy_text", ""),
    }
    document = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>策略回测图形</title>
<style>
body{font-family:Arial,sans-serif;margin:20px;background:#fafafa;color:#222}
canvas{width:100%;height:240px;border:1px solid #ddd;background:white;margin:8px 0}
.meta{white-space:pre-wrap;background:white;padding:12px;border:1px solid #ddd}
</style></head><body>
<h2>__TITLE__</h2><div class="meta">__STRATEGY__</div>
<h3>价格走势与买卖点</h3><canvas id="price" width="1200" height="260"></canvas>
<h3>成交量</h3><canvas id="volume" width="1200" height="180"></canvas>
<h3>MACD</h3><canvas id="macd" width="1200" height="180"></canvas>
<script>
const data=__DATA__;
function line(canvas, values, color, minV, maxV){
 const c=canvas.getContext('2d'),w=canvas.width,h=canvas.height,p=20;
 c.strokeStyle=color;c.beginPath();
 values.forEach((v,i)=>{const x=p+i*(w-2*p)/Math.max(values.length-1,1);
 const y=h-p-(v-minV)*(h-2*p)/Math.max(maxV-minV,1e-9);
 i?c.lineTo(x,y):c.moveTo(x,y)});c.stroke();
}
const closes=data.bars.map(x=>x.close), minP=Math.min(...closes),maxP=Math.max(...closes);
line(document.getElementById('price'),closes,'#2457d6',minP,maxP);
const pc=document.getElementById('price').getContext('2d'),p=20,w=1200,h=260;
for(const t of data.trades){for(const [time,color,label] of [[t.buy_time,'#0a9b55','B'],[t.sell_time,'#d33','S']]){
 const i=data.bars.findIndex(x=>x.time===time);if(i<0)continue;
 const x=p+i*(w-2*p)/Math.max(data.bars.length-1,1),y=h-p-(data.bars[i].close-minP)*(h-2*p)/Math.max(maxP-minP,1e-9);
 pc.fillStyle=color;pc.beginPath();pc.arc(x,y,5,0,Math.PI*2);pc.fill();pc.fillText(label,x+6,y-6);
}}
const vols=data.bars.map(x=>x.volume),maxVol=Math.max(...vols),vc=document.getElementById('volume').getContext('2d');
vols.forEach((v,i)=>{const x=i*1200/vols.length;vc.fillStyle='#94a3b8';vc.fillRect(x,180-v/maxVol*160,Math.max(1,1200/vols.length-1),v/maxVol*160)});
const dif=data.bars.map(x=>x.dif),dea=data.bars.map(x=>x.dea),all=dif.concat(dea),mn=Math.min(...all),mx=Math.max(...all);
line(document.getElementById('macd'),dif,'#d97706',mn,mx);line(document.getElementById('macd'),dea,'#2563eb',mn,mx);
</script></body></html>"""
    document = document.replace(
        "__TITLE__", html.escape(f"{result['symbol']} {result['interval']} 策略回测")
    ).replace(
        "__STRATEGY__", html.escape(result.get("strategy_text", ""))
    ).replace(
        "__DATA__", json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    )
    with open(chart_path, "w", encoding="utf-8") as handle:
        handle.write(document)
    update_chart_path(chart_path)
    return {
        "ok": True,
        "chart_path": chart_path,
        "result_id": result["result_id"],
        "includes": ["价格走势", "买点", "卖点", "成交量", "MACD"],
    }
