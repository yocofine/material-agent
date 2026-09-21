"""Static HTML pages for session login, administrator, and tutoring staff."""
from __future__ import annotations


_STYLE = """
:root { color-scheme: light; font-family: system-ui, 'Microsoft YaHei', sans-serif; }
body { margin: 0; background: #f3f6fa; color: #172033; }
main { max-width: 1100px; margin: 32px auto; padding: 0 18px; }
.card { background: white; border: 1px solid #dce3ec; border-radius: 12px; padding: 22px; margin-bottom: 18px; }
.narrow { max-width: 440px; margin: 80px auto; }
.header { display: flex; justify-content: space-between; align-items: center; gap: 12px; }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 14px; }
label { display: block; margin: 10px 0 5px; font-weight: 650; }
input, select { width: 100%; box-sizing: border-box; padding: 9px; border: 1px solid #b8c3d1; border-radius: 7px; }
button { border: 0; border-radius: 7px; padding: 9px 15px; background: #1769e0; color: white; cursor: pointer; margin: 6px 5px 0 0; }
button.secondary { background: #667085; } button.danger { background: #b42318; }
table { width: 100%; border-collapse: collapse; margin-top: 12px; }
th, td { padding: 9px 7px; border-bottom: 1px solid #e7ebf0; text-align: left; vertical-align: top; }
.muted { color: #667085; }.error { color: #b42318; }.success { color: #067647; }
.badge { display: inline-block; padding: 2px 7px; border-radius: 12px; background: #eef4ff; font-size: 12px; }
@media (max-width: 680px) { .header { align-items: flex-start; flex-direction: column; } table { font-size: 13px; } }
"""


LOGIN_HTML = f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>登录 - material-agent</title>
<style>{_STYLE}</style></head><body><main class="narrow"><div class="card">
<h1>资料管理系统</h1><p class="muted">超级管理员和教辅人员统一登录入口</p>
<form id="loginForm"><label for="username">账号</label><input id="username" autocomplete="username" required>
<label for="password">密码</label><input id="password" type="password" autocomplete="current-password" required>
<button type="submit">登录</button></form><p id="message"></p></div></main>
<script>
document.getElementById('loginForm').addEventListener('submit', async (event) => {{
  event.preventDefault(); const message = document.getElementById('message');
  const response = await fetch('/api/auth/login', {{method:'POST', headers:{{'Content-Type':'application/json'}},
    body:JSON.stringify({{username:document.getElementById('username').value,password:document.getElementById('password').value}})}});
  const data = await response.json();
  if (!response.ok) {{ message.className='error'; message.textContent=data.detail || '登录失败'; return; }}
  location.href = data.redirect;
}});
</script></body></html>"""


_COMMON_SCRIPT = """
async function api(url, options={}) {
  const response = await fetch(url, options);
  if (response.status === 401) { location.href='/login'; throw new Error('登录已过期'); }
  const data = await response.json();
  if (!response.ok) throw new Error(data.detail || '请求失败');
  return data;
}
async function logout() { await fetch('/api/auth/logout', {method:'POST'}); location.href='/login'; }
function formatTime(value) { return value ? new Date(value * 1000).toLocaleString() : '-'; }
"""


STAFF_HTML = f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>教辅工作台</title>
<style>{_STYLE}</style></head><body><main>
<div class="card header"><div><h1>教辅工作台</h1><div id="identity" class="muted"></div></div>
<button class="secondary" onclick="logout()">退出登录</button></div>

<div class="grid"><section class="card"><h2>修改密码</h2>
<label>当前密码</label><input id="currentPassword" type="password" autocomplete="current-password">
<label>新密码</label><input id="newPassword" type="password" autocomplete="new-password">
<label>确认新密码</label><input id="confirmPassword" type="password" autocomplete="new-password">
<button id="changePassword">修改密码</button><p id="passwordMessage"></p></section>

<section class="card"><h2>新增临时客户</h2><p class="muted">正式客户来自业务 MySQL；临时客户仅保存在本系统。</p>
<label>客户编号</label><input id="temporaryId"><label>客户名称</label><input id="temporaryName">
<button id="createTemporary">新增临时客户</button><p id="temporaryMessage"></p></section></div>

<section class="card"><div class="header"><div><h2>我的客户</h2><div id="mysqlStatus" class="muted"></div></div>
<label style="min-width:180px">链接有效时长（小时）<input id="linkHours" type="number" min="0.0167" max="8760" step="0.5" value="24"></label></div>
<table><thead><tr><th>客户编号</th><th>客户名称</th><th>来源</th><th>待处理</th><th>最近上传</th><th>操作</th></tr></thead><tbody id="customers"></tbody></table>
<p id="linkResult"></p></section>

<section class="card"><div class="header"><h2>待分类批次</h2><button id="classifyAll">分类全部</button></div>
<table><thead><tr><th>客户</th><th>批次</th><th>文件数</th><th>上传时间</th><th>操作</th></tr></thead><tbody id="pending"></tbody></table>
<p id="pendingMessage"></p></section></main><script>{_COMMON_SCRIPT}
let principal;
async function loadIdentity() {{ principal=await api('/api/auth/me'); document.getElementById('identity').textContent=`${{principal.display_name}}（${{principal.username}}）`; }}
async function loadCustomers() {{
  const data=await api('/api/staff/customers'); const tbody=document.getElementById('customers'); tbody.innerHTML='';
  const status=document.getElementById('mysqlStatus'); status.textContent=data.official_available ? '正式客户数据已连接' : `正式客户数据暂不可用：${{data.official_error || '未配置'}}`;
  status.className=data.official_available ? 'success' : 'error';
  for (const customer of data.items) {{ const row=document.createElement('tr');
    for (const value of [customer.customer_id,customer.customer_name]) {{ const cell=document.createElement('td'); cell.textContent=value; row.appendChild(cell); }}
    const source=document.createElement('td'); source.innerHTML=`<span class="badge">${{customer.customer_source==='mysql'?'正式':'临时'}}</span>`; row.appendChild(source);
    const pending=document.createElement('td'); pending.textContent=customer.pending_batch_count; row.appendChild(pending);
    const recent=document.createElement('td'); recent.textContent=formatTime(customer.last_upload_at); row.appendChild(recent);
    const action=document.createElement('td'); const button=document.createElement('button'); button.textContent='生成上传链接';
    button.onclick=()=>createLink(customer); action.appendChild(button); row.appendChild(action); tbody.appendChild(row);
  }}
}}
async function createLink(customer) {{ const target=document.getElementById('linkResult');
  try {{ const data=await api('/api/staff/access-links',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{customer_id:customer.customer_id,customer_source:customer.customer_source,expires_in_hours:Number(document.getElementById('linkHours').value)}})}});
    const link=data.url.startsWith('/')?location.origin+data.url:data.url; try {{ await navigator.clipboard.writeText(link); }} catch {{}}
    target.className='success'; target.textContent=`${{customer.customer_name}}：${{link}}`; }} catch(error) {{ target.className='error'; target.textContent=error.message; }}
}}
async function loadPending() {{ const data=await api('/api/pending'); const tbody=document.getElementById('pending'); tbody.innerHTML='';
  for (const item of data.items) {{ const row=document.createElement('tr');
    for (const value of [item.customer_name||item.user_id,item.upload_id,String(item.files),formatTime(item.created)]) {{ const cell=document.createElement('td'); cell.textContent=value; row.appendChild(cell); }}
    const action=document.createElement('td'); const button=document.createElement('button'); button.textContent='分类'; button.onclick=()=>classify(item.upload_id); action.appendChild(button); row.appendChild(action); tbody.appendChild(row); }}
}}
async function classify(uploadId) {{ const message=document.getElementById('pendingMessage'); try {{ message.textContent='正在分类…';
  const data=await api('/api/classify_pending?upload_id='+encodeURIComponent(uploadId),{{method:'POST'}}); message.className='success'; message.textContent=data.message; await loadPending(); await loadCustomers();
}} catch(error) {{ message.className='error'; message.textContent=error.message; }} }}
document.getElementById('classifyAll').onclick=()=>classify('');
document.getElementById('changePassword').onclick=async()=>{{ const message=document.getElementById('passwordMessage'); const current=document.getElementById('currentPassword').value; const next=document.getElementById('newPassword').value;
  if(next!==document.getElementById('confirmPassword').value){{message.className='error';message.textContent='两次新密码不一致';return;}}
  try{{await api('/api/staff/me/password',{{method:'PATCH',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{current_password:current,new_password:next}})}});alert('密码已修改，请使用新密码重新登录');location.href='/login';}}catch(error){{message.className='error';message.textContent=error.message;}}
}};
document.getElementById('createTemporary').onclick=async()=>{{ const message=document.getElementById('temporaryMessage'); try{{await api('/api/staff/customers/temporary',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{customer_id:document.getElementById('temporaryId').value,customer_name:document.getElementById('temporaryName').value}})}});message.className='success';message.textContent='临时客户已创建';await loadCustomers();}}catch(error){{message.className='error';message.textContent=error.message;}} }};
Promise.all([loadIdentity(),loadCustomers(),loadPending()]);
</script></body></html>"""


ADMIN_HTML = f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>超级管理员</title><style>{_STYLE}</style></head><body><main>
<div class="card header"><div><h1>超级管理员</h1><div id="identity" class="muted"></div></div><button class="secondary" onclick="logout()">退出登录</button></div>
<section class="card"><h2>创建教辅账号</h2><div class="grid"><div><label>登录名</label><input id="username"></div><div><label>教辅姓名</label><input id="displayName"></div><div><label>初始密码</label><input id="password" type="password"></div></div><button id="createStaff">创建账号</button><p id="message"></p></section>
<section class="card"><h2>教辅账号</h2><table><thead><tr><th>姓名</th><th>登录名</th><th>状态</th><th>操作</th></tr></thead><tbody id="staff"></tbody></table></section>
<section class="card"><div class="header"><h2>全部待分类批次</h2><button id="classifyAll">分类全部</button></div><div id="pending"></div><p id="pendingMessage"></p></section>
</main><script>{_COMMON_SCRIPT}
async function loadIdentity() {{ const me=await api('/api/auth/me'); document.getElementById('identity').textContent=`${{me.display_name}}（${{me.username}}）`; }}
async function loadStaff() {{ const data=await api('/api/admin/staff'); const tbody=document.getElementById('staff'); tbody.innerHTML=''; for(const staff of data.items){{const row=document.createElement('tr');
  for(const value of [staff.display_name,staff.username,staff.active?'启用':'停用']){{const cell=document.createElement('td');cell.textContent=value;row.appendChild(cell);}}
  const action=document.createElement('td'); const toggle=document.createElement('button');toggle.textContent=staff.active?'停用':'启用';toggle.className=staff.active?'danger':'';toggle.onclick=async()=>{{await api('/api/admin/staff/'+encodeURIComponent(staff.staff_id),{{method:'PATCH',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{active:!staff.active}})}});await loadStaff();}};
  const reset=document.createElement('button');reset.textContent='重置密码';reset.className='secondary';reset.onclick=async()=>{{const password=prompt('输入至少10位的新密码');if(!password)return;await api('/api/admin/staff/'+encodeURIComponent(staff.staff_id),{{method:'PATCH',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{password}})}});alert('密码已重置，该教辅需要重新登录');}};action.append(toggle,reset);row.appendChild(action);tbody.appendChild(row);}} }}
async function loadPending() {{const data=await api('/api/pending');document.getElementById('pending').textContent=data.items.length?data.items.map(x=>`${{x.staff_name||'-'}} / ${{x.customer_name||x.user_id}} / ${{x.upload_id}}（${{x.files}}个文件）`).join('\\n'):'暂无待分类批次';}}
document.getElementById('createStaff').onclick=async()=>{{const message=document.getElementById('message');try{{await api('/api/admin/staff',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{username:document.getElementById('username').value,display_name:document.getElementById('displayName').value,password:document.getElementById('password').value}})}});message.className='success';message.textContent='账号已创建';document.getElementById('password').value='';await loadStaff();}}catch(error){{message.className='error';message.textContent=error.message;}}}};
document.getElementById('classifyAll').onclick=async()=>{{const message=document.getElementById('pendingMessage');try{{const data=await api('/api/classify_pending',{{method:'POST'}});message.className='success';message.textContent=data.message;await loadPending();}}catch(error){{message.className='error';message.textContent=error.message;}}}};
Promise.all([loadIdentity(),loadStaff(),loadPending()]);
</script></body></html>"""
