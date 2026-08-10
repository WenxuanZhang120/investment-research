import portfolioJson from "@/generated/portfolio-snapshot.json";
import researchJson from "@/generated/research-snapshot.json";
import { auth } from "@/auth";
import { loginWithGitHub } from "@/app/actions";
import ResearchDashboard from "@/app/research-dashboard";
import { getDecisionLogs } from "@/lib/decision-log";
import type { ResearchSnapshot } from "@/lib/research-types";
import type { PortfolioSnapshot } from "@/lib/types";

export const dynamic = "force-dynamic";

const portfolio = portfolioJson as PortfolioSnapshot;
const research = researchJson as ResearchSnapshot;

function LoginScreen() {
  return <main className="login-shell light-login">
    <section className="login-intro">
      <div className="login-brand"><span>IR</span><div><strong>投资研究台</strong><small>PRIVATE RESEARCH OS</small></div></div>
      <div className="login-copy-block"><p>从数据到决策</p><h1>把全市场研究，<br />放进一张清晰的桌面。</h1><p>筛选、排序和过滤全市场候选；查看单只证券的行情、财务与评分；阅读日报、新闻、公告和研究报告；最后把判断写进私有决策日志。</p></div>
      <div className="login-capabilities"><span><b>{research.coverage.screeningTotal.toLocaleString("zh-CN")}</b> 条筛选结果</span><span><b>{research.coverage.financialSecurityCount.toLocaleString("zh-CN")}</b> 只证券财务</span><span><b>{research.content.length}</b> 条研究内容</span></div>
    </section>
    <section className="login-panel">
      <div className="login-panel-inner"><span className="login-lock">PRIVATE ACCESS</span><h2>进入综合投资研究看板</h2><p>该站点保存个人持仓与决策日志，只允许指定 GitHub 账户访问。</p><form action={loginWithGitHub}><button className="github-login-button" type="submit"><span>GH</span>使用 GitHub 登录 <b>→</b></button></form><small>仅允许 <strong>WenxuanZhang120</strong></small><div className="login-data-note"><i /><span>数据源固定为 GitHub 分支<br /><strong>codex/github-connector-small-files</strong></span></div></div>
    </section>
  </main>;
}

export default async function Home() {
  const session = await auth();
  const allowedLogin = process.env.ALLOWED_GITHUB_LOGIN ?? "WenxuanZhang120";
  if (session?.user?.githubLogin?.toLowerCase() !== allowedLogin.toLowerCase()) return <LoginScreen />;

  const logs = await getDecisionLogs();
  return <ResearchDashboard
    user={{ name: session.user.name ?? "Wenxuan", login: session.user.githubLogin ?? allowedLogin }}
    portfolio={portfolio}
    research={research}
    logs={{
      available: logs.available,
      message: logs.message,
      entries: logs.entries.map((entry) => ({ ...entry, createdAt: entry.createdAt.toISOString(), updatedAt: entry.updatedAt.toISOString() })),
    }}
  />;
}
