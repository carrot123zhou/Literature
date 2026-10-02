ObjC.import("Foundation");

function shellQuote(value) {
  return "'" + value.replace(/'/g, "'\"'\"'") + "'";
}

function run() {
  const application = Application.currentApplication();
  application.includeStandardAdditions = true;

  const bundleUrl = $.NSBundle.mainBundle.bundleURL;
  const launcherUrl = bundleUrl.URLByDeletingLastPathComponent;
  const projectUrl = launcherUrl.URLByDeletingLastPathComponent;
  const scriptUrl = projectUrl.URLByAppendingPathComponent("scripts/start_webui.sh");
  const scriptPath = ObjC.unwrap(scriptUrl.path);

  application.doShellScript(
    "/bin/zsh " + shellQuote(scriptPath) + " >/dev/null 2>&1 &"
  );
}
