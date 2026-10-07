// AI Talk - Cloudflare Worker
// 화면 파일(index.html)은 그대로 보여 주고, /firebase-config.json 요청에만
// Cloudflare 의 'Variables and Secrets' 에 넣은 Firebase 설정을 돌려준다.
// 그래서 Firebase 주소를 GitHub 코드에 넣지 않고도, 배포된 주소로 들어온 누구나 같은 Firebase 를 쓴다.

const FIELDS = {   // Firebase 설정 이름 -> Cloudflare 변수 이름
  apiKey: 'FIREBASE_API_KEY',
  authDomain: 'FIREBASE_AUTH_DOMAIN',
  databaseURL: 'FIREBASE_DATABASE_URL',
  projectId: 'FIREBASE_PROJECT_ID',
  storageBucket: 'FIREBASE_STORAGE_BUCKET',
  messagingSenderId: 'FIREBASE_MESSAGING_SENDER_ID',
  appId: 'FIREBASE_APP_ID',
};

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname === '/firebase-config.json') return firebaseConfig(env);
    return env.ASSETS.fetch(request);
  },
};

// FIREBASE_CONFIG 하나에 Firebase 콘솔의 firebaseConfig 를 통째로 붙여 넣어도 되고 (JSON 또는 JS 객체 모양),
// FIREBASE_API_KEY, FIREBASE_DATABASE_URL … 처럼 하나씩 넣어도 된다.
function firebaseConfig(env) {
  const config = {};
  if (env.FIREBASE_CONFIG) {
    for (const [, key, value] of String(env.FIREBASE_CONFIG).matchAll(/["']?(\w+)["']?\s*:\s*["']([^"']*)["']/g)) {
      if (key in FIELDS) config[key] = value;
    }
  }
  for (const [key, name] of Object.entries(FIELDS)) {
    if (env[name]) config[key] = String(env[name]).trim();
  }
  if (config.databaseURL) config.databaseURL = config.databaseURL.replace(/\/+$/, '');
  if (env.ADMIN_ID) config.adminId = String(env.ADMIN_ID).trim();   // 관리자 아이디 (쉼표로 여러 명). /admin 화면을 보여 줄 사람
  if (!config.authDomain && config.projectId) config.authDomain = `${config.projectId}.firebaseapp.com`;
  if (!config.projectId && config.authDomain) config.projectId = config.authDomain.split('.')[0];

  const headers = { 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store' };
  if (!config.apiKey || !config.databaseURL) {
    return new Response(JSON.stringify({
      error: 'Cloudflare 의 Variables and Secrets 에 FIREBASE_API_KEY 와 FIREBASE_DATABASE_URL (또는 FIREBASE_CONFIG) 를 넣어 주세요.',
    }), { status: 404, headers });
  }
  return new Response(JSON.stringify(config), { headers });
}
