// Read-only facade around the pinned candidate SDK. No broad OneBot server or poller is started.
import {createServer} from 'node:http';
import {timingSafeEqual} from 'node:crypto';
import {chmodSync, existsSync, readFileSync} from 'node:fs';
import {fileURLToPath} from 'node:url';
import {resolve, dirname} from 'node:path';
import dotenv from '../data/bridge/node_modules/dotenv/lib/main.js';
import {QzoneClient} from '../data/bridge/src/qzone/client.ts';

const root=resolve(dirname(fileURLToPath(import.meta.url)),'..');
dotenv.config({path:resolve(root,'data/bridge/.env')});
const expected=process.env.ONEBOT_ACCESS_TOKEN||'';
if(expected.length<24)throw Error('缺少桥接专用令牌，请运行 install_bridge.py');
const client=new QzoneClient({cachePath:resolve(root,'data/bridge-session')});
const local=JSON.parse(readFileSync(resolve(root,'data/local-config.json'),'utf8'));
const loginQQ=String(local.login_qq||'');
const targets=new Set((local.sources||[]).map(s=>String(s.qzone_id||'')));
if(!/^[1-9][0-9]{4,14}$/.test(loginQQ)||targets.size!==2||[...targets].some(q=>!/^[1-9][0-9]{4,14}$/.test(q))){
  throw Error('请先填写本机 data/local-config.json 的小号和两个来源');
}
if(client.loggedIn&&client.qqNumber!==loginQQ)throw Error('缓存登录账号与本机配置不一致，请人工核对会话');
const send=(res,status,data)=>{res.writeHead(status,{'Content-Type':'application/json','Cache-Control':'no-store'});res.end(JSON.stringify(data));};
const ok=data=>({status:'ok',retcode:0,data});
const fail=retcode=>({status:'failed',retcode,data:null,message:retcode===1401?'会话不可用，请人工登录':'只读桥接请求未完成'});
let busy=false;
const server=createServer(async(req,res)=>{
  const auth=(req.headers.authorization||'').replace(/^Bearer /,'');
  if(auth.length!==expected.length||!timingSafeEqual(Buffer.from(auth),Buffer.from(expected))){send(res,401,fail(1401));return;}
  if(req.method!=='POST'){send(res,405,fail(1400));return;}
  if(!['/check_cookie','/get_login_info','/get_emotion_list'].includes(req.url)){send(res,404,fail(1404));return;}
  if(busy){send(res,200,fail(1429));return;}
  try{
    let text='';for await(const chunk of req){text+=chunk;if(text.length>16384){send(res,413,fail(1400));return;}}
    const p=JSON.parse(text||'{}');busy=true;
    if(req.url==='/check_cookie'){
      const valid=client.loggedIn&&await client.validateSession(true);
      send(res,200,ok({valid,expired:!valid}));return;
    }
    if(!client.loggedIn){send(res,200,fail(1401));return;}
    if(req.url==='/get_login_info'){send(res,200,ok({user_id:client.qqNumber}));return;}
    if(!targets.has(String(p.user_id))){send(res,403,fail(1403));return;}
    // Read the actual target's visible timeline. The candidate SDK's getPostsStrict
    // uses a filtered friend stream and its continuation failed in this account's probe.
    const cursor=String(p.cursor||'offset:0');
    if(!/^offset:\d+$/.test(cursor)){send(res,400,fail(1400));return;}
    const start=Number(cursor.slice(7));
    if(!Number.isSafeInteger(start)||start>10000){send(res,400,fail(1400));return;}
    const result=await client.getFeedsHtmlActAll(String(p.user_id),{hostUin:String(p.user_id),start,count:20,scope:0});
    if(Number(result.code)!==0){send(res,200,fail(client.isAuthFailure(result)?1401:1503));return;}
    if(!Array.isArray(result.msglist)||typeof result.has_more!=='boolean'){send(res,200,fail(1503));return;}
    send(res,200,ok({msglist:result.msglist,has_more:result.has_more,
      next_cursor:result.has_more?'offset:'+result.next_start:null,
      source:'feeds_html_act_all',reliability:'strict',warnings:[],pagination:'offset',raw_count:result.msglist.length}));
  }catch{send(res,200,fail(1500));}finally{busy=false;}
});

async function main(){
  if(process.argv.includes('--login')){
    if(client.loggedIn&&await client.validateSession(true)){console.log('现有桥接会话有效，无需重新登录。');return;}
    // Only the SDK's explicit QR login is enabled; challenge handling remains manual.
    console.log('正在打开 QQ 空间登录窗口，请使用本机配置的专用小号登录。二维码将保存在 data/bridge-session/qrcode.png。');
    await client.loginWithPlaywright(300,false);
    if(client.qqNumber!==loginQQ){client.logout();throw Error('登录账号不符合专用小号配置');}
    const cookieFile=resolve(root,'data/bridge-session/cookies.json');
    if(existsSync(cookieFile))chmodSync(cookieFile,0o600);
    console.log('专用小号登录完成，会话仅保存在本机。');return;
  }
  server.listen(5700,'127.0.0.1',()=>console.log('只读桥接已启动：127.0.0.1:5700；仅允许两个目标来源。'));
}
main().catch(()=>{console.error('桥接操作未完成：请检查登录窗口、网络或本机配置。');process.exitCode=1;});
