import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "投资研究台｜综合投资研究看板",
  description: "全市场筛选、证券研究、内容阅读、个人持仓与决策日志",
  robots: { index: false, follow: false },
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return <html lang="zh-CN"><body>{children}</body></html>;
}
