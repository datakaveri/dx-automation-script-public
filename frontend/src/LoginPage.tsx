interface LoginPageProps {
  onLogin: () => void;
  error?: string | null;
}

export function LoginPage({ onLogin, error }: LoginPageProps) {
  return (
    <div className="flex min-h-screen items-center justify-center bg-slate-100 px-4 py-12 sm:px-6 lg:px-8">
      <div className="w-full max-w-md space-y-6 rounded-2xl border border-slate-200 bg-white p-8 shadow-xl">
        <div className="text-center">
          <h2 className="text-2xl font-black tracking-tight text-slate-900">API Testing Platform</h2>
          <p className="mt-2 text-sm text-slate-600">Sign in with your IUDX account.</p>
        </div>
        {error && <div role="alert" className="rounded-lg border border-rose-200 bg-rose-50 p-3 text-xs font-semibold text-rose-800">{error}</div>}
        <button type="button" onClick={onLogin} className="flex w-full justify-center rounded-lg bg-slate-900 py-2.5 text-sm font-bold text-white shadow-md transition hover:bg-slate-800 active:bg-slate-700">
          Sign in with Keycloak
        </button>
      </div>
    </div>
  );
}
