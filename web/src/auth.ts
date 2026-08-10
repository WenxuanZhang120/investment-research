import NextAuth from "next-auth";
import GitHub from "next-auth/providers/github";

const allowedLogin = (process.env.ALLOWED_GITHUB_LOGIN ?? "WenxuanZhang120").toLowerCase();

export const { handlers, auth, signIn, signOut } = NextAuth({
  providers: [GitHub],
  pages: { signIn: "/" },
  callbacks: {
    async signIn({ account, profile }) {
      if (account?.provider !== "github" || !profile || !("login" in profile)) return false;
      return String(profile.login).toLowerCase() === allowedLogin;
    },
    async jwt({ token, account, profile }) {
      if (account?.provider === "github" && profile && "login" in profile) {
        token.githubLogin = String(profile.login);
      }
      return token;
    },
    async session({ session, token }) {
      if (session.user) session.user.githubLogin = String(token.githubLogin ?? "");
      return session;
    },
    authorized({ auth: session }) {
      return session?.user?.githubLogin?.toLowerCase() === allowedLogin;
    },
  },
});
