const { chromium } = require(process.env.PW_PACKAGE_PATH);
const web = `http://127.0.0.1:${process.argv[2]}`, api = `http://127.0.0.1:${process.argv[3]}`, id = process.argv[4];
async function main() {
 const checks=[]; const browser=await chromium.launch({headless:true}); const page=await browser.newPage({viewport:{width:1500,height:950}}); const errors=[];
 page.on('pageerror', e => errors.push(String(e)));
 const graph=async()=>{const r=await fetch(`${api}/projects/${id}/document`, {headers:{"x-literature-token":""}});if(!r.ok)throw Error(`graph ${r.status}`);return r.json()};
 await page.goto(`${web}/#/p/${id}/review`,{waitUntil:'networkidle'}); await page.getByText('结构编辑',{exact:true}).waitFor();
 const cards=page.locator('.block-card'); await cards.filter({hasText:'Body paragraph 0.'}).getByRole('checkbox').check(); await cards.filter({hasText:'Body paragraph 1.'}).getByRole('checkbox').check();
 await page.getByRole('button',{name:'合并块'}).click(); checks.push(['合并预览含两块', (await page.getByRole('dialog').innerText()).includes('Body paragraph 0. Body paragraph 1.')]); await page.getByRole('button',{name:'确认合并'}).click();
 await page.waitForTimeout(500); let d=await graph(); checks.push(['合并写入规范文档',d.blocks.some(b=>b.source_text==='Body paragraph 0. Body paragraph 1.')]);
 await page.getByRole('button',{name:'撤销',exact:true}).first().click(); await page.waitForTimeout(400); d=await graph(); checks.push(['撤销恢复两块',d.blocks.some(b=>b.block_id==='p0')&&d.blocks.some(b=>b.block_id==='p1')]);
 await page.getByRole('button',{name:'重做',exact:true}).click(); await page.waitForTimeout(400); d=await graph(); checks.push(['重做合并',d.blocks.some(b=>b.source_text==='Body paragraph 0. Body paragraph 1.')]);
 const merged=cards.filter({hasText:'Body paragraph 0. Body paragraph 1.'}); await merged.click(); await merged.getByRole('button',{name:'拆分块'}).click();
 const split=page.getByRole('textbox',{name:'选择拆分位置'}); await split.evaluate(el=>{el.focus();el.setSelectionRange(17,17);el.dispatchEvent(new KeyboardEvent('keyup',{bubbles:true,key:'ArrowRight'}));}); await page.getByRole('button',{name:'确认拆分'}).click(); await page.waitForTimeout(400); d=await graph(); checks.push(['选择位置后拆分',d.blocks.some(b=>b.source_text==='Body paragraph 0.') && d.blocks.some(b=>b.source_text===' Body paragraph 1.')]);
 let first=cards.filter({hasText:'Body paragraph 0.'}).first(); await first.click(); await first.getByRole('button',{name:'编辑内容'}).click(); await page.getByRole('dialog').getByRole('textbox').fill('A😀B'); await page.getByRole('button',{name:'确认保存'}).click(); await page.waitForTimeout(400); d=await graph(); checks.push(['编辑原文',d.blocks.some(b=>b.source_text==='A😀B')]);
 const emojiCard=cards.filter({hasText:'A😀B'}).first(); await emojiCard.click(); await emojiCard.getByRole('button',{name:'拆分块'}).click(); const emojiSplit=page.getByRole('textbox',{name:'选择拆分位置'}); await emojiSplit.evaluate(el=>{el.focus();el.setSelectionRange(3,3);el.dispatchEvent(new KeyboardEvent('keyup',{bubbles:true,key:'ArrowRight'}));}); await page.getByRole('button',{name:'确认拆分'}).click(); await page.waitForTimeout(400); d=await graph(); checks.push(['Emoji 后按字符位置拆分',d.blocks.some(b=>b.source_text==='A😀') && d.blocks.some(b=>b.source_text==='B')]);
 await page.getByRole('checkbox',{name:'选择目录 Chapter 0'}).check(); await page.getByRole('checkbox',{name:'选择目录 Chapter 1'}).check(); await page.getByRole('combobox',{name:'批量调整目录层级'}).selectOption('2'); await page.waitForTimeout(400); d=await graph(); checks.push(['目录批量层级',d.blocks.filter(b=>['t0','t1'].includes(b.block_id)).every(b=>b.level===2)]);
 await page.keyboard.press('Meta+z'); await page.waitForTimeout(400); d=await graph(); checks.push(['快捷撤销',d.blocks.filter(b=>['t0','t1'].includes(b.block_id)).every(b=>b.level===1)]); await page.keyboard.press('Meta+Shift+z'); await page.waitForTimeout(400); d=await graph(); checks.push(['快捷重做',d.blocks.filter(b=>['t0','t1'].includes(b.block_id)).every(b=>b.level===2)]);

 // 真正跨页合并后，每个来源都能跳转到对应的原页及其框。
 const p2=cards.filter({hasText:'Body paragraph 2.'}).first(), p3=cards.filter({hasText:'Body paragraph 3.'}).first();
 await p2.getByRole('checkbox').check(); await p3.getByRole('checkbox').check();
 await page.getByRole('button',{name:'合并块'}).click(); await page.getByRole('button',{name:'确认合并'}).click(); await page.waitForTimeout(1000);
 const mergedCross=cards.filter({hasText:'Body paragraph 2. Body paragraph 3.'}).first(); await mergedCross.click();
 const source1=mergedCross.getByRole('button',{name:'第 1 页 · 来源 1'}), source2=mergedCross.getByRole('button',{name:'第 2 页 · 来源 2'});
 checks.push(['合并块展示两个来源',await source1.isVisible() && await source2.isVisible()]);
 for (const [button, pageIndex, sourceIndex] of [[source2,1,1],[source1,0,0]]) {
   await button.click(); await page.waitForTimeout(1600);
   const visible=await page.evaluate(({pageIndex,sourceIndex})=>{const panel=document.querySelector('.pdf-scroll'), box=document.querySelector(`.pdf-page[data-page-index="${pageIndex}"] .bbox[data-block-id="p2"][data-source-index="${sourceIndex}"]`); if(!panel||!box)return false;const a=box.getBoundingClientRect(),b=panel.getBoundingClientRect();return a.bottom>b.top && a.top<b.bottom && Math.abs((a.top+a.bottom-b.top-b.bottom)/2)<120;},{pageIndex,sourceIndex});
   checks.push([`来源 ${sourceIndex+1} 定位对应页的框`,visible]);
   if (sourceIndex===1) { await page.locator('.pdf-page[data-page-index="1"] .bbox[data-block-id="p2"][data-source-index="1"]').click(); await page.waitForTimeout(900); const current=await page.locator('.pdf-panel').getAttribute('data-current-page'); checks.push(['点击第二页的框保留当前页',current==='2']); }
 }
 checks.push(['无页面异常',errors.length===0]); await browser.close(); return {checks,errors};
}
main().then(x=>console.log(JSON.stringify(x))).catch(e=>{console.log(JSON.stringify({checks:[],error:String(e.stack||e)}));process.exit(2)});
