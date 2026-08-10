import "next-auth";
import "next-auth/jwt";

declare module "next-auth" {
  interface User {
    githubLogin?: string;
  }

  interface Session {
    user: {
      githubLogin: string;
      name?: string | null;
      email?: string | null;
      image?: string | null;
    };
  }
}

declare module "next-auth/jwt" {
  interface JWT {
    githubLogin?: string;
  }
}
