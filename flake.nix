{
  description = "flake-query — inspect Nix flake installables for build/closure/cache metadata";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixpkgs-unstable";

  outputs = { self, nixpkgs }:
    let
      forAllSystems = nixpkgs.lib.genAttrs nixpkgs.lib.systems.flakeExposed;
    in
    {
      apps = forAllSystems (system: {
        default = {
          type = "app";
          program = "${self.packages.${system}.default}/bin/flake-query";
        };
      });

      packages = forAllSystems (system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
        in
        {
          default = pkgs.writeShellApplication {
            name = "flake-query";
            runtimeInputs = with pkgs; [ nix jq python3 ];
            text = ''
              exec python3 ${./flake-query.py} "$@"
            '';
          };
        });
    };
}
