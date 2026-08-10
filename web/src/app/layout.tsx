import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "投资工作台",
  description: "个人持仓与投资决策日志",
  robots: { index: false, follow: false },
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return <html lang="zh-CN"><body>{children}</body></html>;
}
