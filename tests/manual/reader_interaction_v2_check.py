"""阅读交互验收：生产前端 + 隔离 API + 受控模型，禁止写真实 Keychain。"""
import os,sys,tempfile,threading,time,glob,subprocess,json
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
from tests.manual.engine_ui_browser_check import free_port,prepare_dist,start_web
from tests.test_reader_semantics import SemanticProvider
with tempfile.TemporaryDirectory() as temp:
    base=Path(temp);(base/'runtime').mkdir()
    os.environ.update(LITERATURE_RUNTIME=str(base/'runtime'),LITERATURE_LIBRARY=str(base/'library'),LITERATURE_SESSION_TOKEN='')
    (base/'runtime/session-token').write_text('')
    import literature.model_services as ms
    ms.save_service_key=lambda *a,**k:None
    ms.delete_service_key=lambda *a,**k:None
    ms.ModelServiceStore.api_key=lambda self,sid:'isolated-fixture-key'
    from tests.manual.reader_ui_browser_check import seed_project
    store,pid=seed_project(base)
    # Existing readers often have all three panes enabled, exactly matching the
    # frontend defaults. Listener binding must happen when the async body mounts,
    # even if no pane preference changes (WKWebView production regression).
    from literature.reader import ReaderStore
    ReaderStore(store).save_overlay(pid, {'prefs':{'panes':{'origin':True,'source':True,'trans':True}}})
    from literature.api import create_app
    import uvicorn
    app=create_app(store);provider=SemanticProvider()
    app.state.job_manager.provider_from=lambda *a,**k:provider
    ap,wp=free_port(),free_port()
    server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=ap,log_level='error'))
    thread=threading.Thread(target=server.run,daemon=True);thread.start()
    for _ in range(100):
        if server.started:break
        time.sleep(.1)
    web=start_web(prepare_dist(base,ap),wp)
    try:
        cache=str(Path.home()/'Library/Caches/ms-playwright-go')
        node=sorted(glob.glob(cache+'/*/node'))[-1];pkg=sorted(glob.glob(cache+'/*/package'))[-1]
        result=subprocess.run([node,str(ROOT/'tests/manual/reader_interaction_v2_check.cjs'),str(wp),str(ap),pid],env={**os.environ,'PW_PACKAGE_PATH':pkg},capture_output=True,text=True,timeout=100)
        print(result.stdout);print(result.stderr[-2000:]);print('provider_calls',len(provider.calls))
        (ROOT/'tmp/reader-new-interactions-result.txt').write_text(result.stdout+'\n'+result.stderr)
        sys.exit(result.returncode)
    finally:
        server.should_exit=True;web.shutdown();thread.join(timeout=5)
