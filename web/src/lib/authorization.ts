import { auth } from "@/auth";

export class UnauthorizedError extends Error {
  constructor() {
    super("未通过 GitHub 白名单验证");
    this.name = "UnauthorizedError";
  }
}

export async function requireAllowedUser() {
  const session = await auth();
  const login = session?.user?.githubLogin;
  const allowed = process.env.ALLOWED_GITHUB_LOGIN ?? "WenxuanZhang120";
  if (!login || login.toLowerCase() !== allowed.toLowerCase()) throw new UnauthorizedError();
  return { session, login };
}
