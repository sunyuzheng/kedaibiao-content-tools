import type { Metadata } from "next";
import { Geist, Geist_Mono } from "next/font/google";
import { headers } from "next/headers";
import "./globals.css";

const geistSans = Geist({
  variable: "--font-geist-sans",
  subsets: ["latin"],
});

const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
});

const description =
  "本地交互式决策台：审查 YouTube → Transistor 同步状态、待发布候选、blocked 项和元数据缺口。";

export async function generateMetadata(): Promise<Metadata> {
  const requestHeaders = await headers();
  const host =
    requestHeaders.get("x-forwarded-host") ??
    requestHeaders.get("host") ??
    "localhost:3000";
  const protocol =
    requestHeaders.get("x-forwarded-proto") ??
    (host.startsWith("localhost") || host.startsWith("127.0.0.1")
      ? "http"
      : "https");
  const imageUrl = new URL(
    "/podcast-decision-dashboard-og.png",
    `${protocol}://${host}`,
  ).toString();

  return {
    title: "课代表播客 · 决策面板",
    description,
    openGraph: {
      title: "课代表播客 · 决策面板",
      description,
      type: "website",
      locale: "zh_CN",
      images: [
        {
          url: imageUrl,
          width: 1200,
          height: 630,
          alt: "课代表播客同步决策面板：33 可发布、20 blocked、35 个描述缺口",
        },
      ],
    },
    twitter: {
      card: "summary_large_image",
      title: "课代表播客 · 决策面板",
      description,
      images: [imageUrl],
    },
  };
}

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="zh-CN">
      <body
        className={`${geistSans.variable} ${geistMono.variable} antialiased`}
      >
        {children}
      </body>
    </html>
  );
}
