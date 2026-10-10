# Application shell

`page-host.ts` owns workspace navigation, shell visibility and one mounted
page lifecycle. Feature pages receive a container and explicit ports; they
must not reach into chat chrome. Legacy Paper/Research adapters retain their
domain lifecycle while delegating shared visibility here.

`fragments/` contains server-composed shell markup; each fragment has exactly
one `APPLICATION_SHELL_FRAGMENT` marker in root `index.html`.
Verify with application-shell fragment tests, workspace browser journeys,
`npm run check:frontend`, and the frontend architecture contract.

`interactions/` owns sidebar gestures, composer input, file drops, chrome
geometry, shell shortcuts and viewport height through explicit ports. It is
constructed only by retained boot and disposed by the composition scope.
Domain actions stay in their existing owners; constructors start no requests.
Verify with `node --test tests/application_interactions.test.cjs` and
`tests/visual/test_visual_application_interactions.py`, then the shared
lifecycle/sync browser contracts and the frontend gate.

`model-picker/` projects generated routing DTOs and owns composer model menu
DOM, its render-local search index and one scoped provider popup. Inject the
list host, selection and domain actions; never read or write model persistence
here. Shared controls come from UI factories. Verify with `check:model-picker`,
model ordering/fold tests and the model-picker/startup browser contracts.
