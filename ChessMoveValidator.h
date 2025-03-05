#ifndef CHESSMOVEVALIDATOR_H
#define CHESSMOVEVALIDATOR_H

#include <string>
#include <map>
#include <vector>
#include <cmath>

class ChessMoveValidator{
public:

    ChessMoveValidator(const std::array<std::string, 65>& fen) {
        updateFEN(fen);
    }

    bool is_legal_move(const std::string& uci_move) {

        if (uci_move.length() < 4) 
        {
            return false;
        } 
        
        std::string from_sq = uci_move.substr(0, 2);
        std::string to_sq = uci_move.substr(2, 2);
        char promotion = uci_move.length() == 5 ? uci_move[4] : '\0';
        
        int from = square_to_index(from_sq);
        int to = square_to_index(to_sq);

        char piece = board_[from];
        char target = board_[to];
        
        if (piece == '0' || !is_valid_piece(piece)) return false;
        if (!is_correct_turn(piece)) return false;
        if (!is_valid_destination(piece, from, to, target)) return false;
        if (would_cause_check(from, to)) return false;

        return true;
    }

    void updateFEN(const std::array<std::string, 65>& fen) {        
        parseFEN(fen);
    }


private:


    std::map<std::string, int> field_to_idx = 
        {
            {"a8", 0}, {"b8", 1}, {"c8", 2}, {"d8", 3}, {"e8", 4}, {"f8", 5}, {"g8", 6}, {"h8", 7},
            {"a7", 8}, {"b7", 9}, {"c7", 10}, {"d7", 11}, {"e7", 12}, {"f7", 13}, {"g7", 14}, {"h7", 15},
            {"a6", 16}, {"b6", 17}, {"c6", 18}, {"d6", 19}, {"e6", 20}, {"f6", 21}, {"g6", 22}, {"h6", 23},
            {"a5", 24}, {"b5", 25}, {"c5", 26}, {"d5", 27}, {"e5", 28}, {"f5", 29}, {"g5", 30}, {"h5", 31},
            {"a4", 32}, {"b4", 33}, {"c4", 34}, {"d4", 35}, {"e4", 36}, {"f4", 37}, {"g4", 38}, {"h4", 39},
            {"a3", 40}, {"b3", 41}, {"c3", 42}, {"d3", 43}, {"e3", 44}, {"f3", 45}, {"g3", 46}, {"h3", 47},
            {"a2", 48}, {"b2", 49}, {"c2", 50}, {"d2", 51}, {"e2", 52}, {"f2", 53}, {"g2", 54}, {"h2", 55},
            {"a1", 56}, {"b1", 57}, {"c1", 58}, {"d1", 59}, {"e1", 60}, {"f1", 61}, {"g1", 62}, {"h1", 63}
        };

    char board_[64];
    bool white_to_move_;
    bool castling_rights[4]; // KQkq castling rights

    void parseFEN(const std::array<std::string, 65>& fen) {
        // update who to move
        white_to_move_ = (bool)(fen[64]=="w");

        for (int i = 0; i < 64; i++) {
            board_[i] = fen[i][0]; // first char only
         };
    }

    int square_to_index(const std::string& sq) {
        return field_to_idx[sq];
    }

    bool is_valid_piece(char piece) {
        return std::string("KQRBNPkqrbnp").find(piece) != std::string::npos;
    }
    
    bool is_correct_turn(char piece) {
        return (isupper(piece) && white_to_move_) || (islower(piece) && !white_to_move_);
    }

    bool is_valid_destination(char piece, int from, int to, char target) {
        if (target != '0' && ((isupper(piece) && isupper(target)) || (islower(piece) && islower(target))))
            return false;
        return is_valid_piece_movement(piece, from, to);
    }

    bool is_valid_piece_movement(char piece, int from, int to) {
        int dx = abs((from % 8) - (to % 8));
        int dy = abs((from / 8) - (to / 8));
        
        switch (tolower(piece)) {
            case 'p': return is_valid_pawn_move(piece, from, to);
            case 'n': return (dx == 2 && dy == 1) || (dx == 1 && dy == 2);
            case 'b': return dx == dy && !is_obstructed(from, to);
            case 'r': return (dx == 0 || dy == 0) && !is_obstructed(from, to);
            case 'q': return (dx == dy || dx == 0 || dy == 0) && !is_obstructed(from, to);
            case 'k': return is_valid_king_move(from, to);
        }
        return false;
    }

    bool is_valid_pawn_move(char piece, int from, int to) {
        int direction = isupper(piece) ? -8 : 8;
        if (to == from + direction && board_[to] == '0') return true;
        if ((from / 8 == (isupper(piece) ? 6 : 1)) && to == from + 2 * direction && board_[from + direction] == '0' && board_[to] == '0') return true;
        if ((to == from + direction - 1 || to == from + direction + 1) && board_[to] != '0' && isupper(piece) != isupper(board_[to])) return true;
        return false;
    }

    bool is_obstructed(int from, int to) {
        int dx = (to % 8) - (from % 8), dy = (to / 8) - (from / 8);
        int step_x = dx ? dx / abs(dx) : 0, step_y = dy ? dy / abs(dy) : 0;
        int x = from % 8 + step_x, y = from / 8 + step_y;
        
        while ((x != to % 8 || y != to / 8) && (x >= 0 && x < 8 && y >= 0 && y < 8)) {
            if (board_[y * 8 + x] != '0') return true;
            x += step_x;
            y += step_y;
        }
        return false;
    }

    bool is_valid_king_move(int from, int to) {
        int dx = abs((from % 8) - (to % 8));
        int dy = abs((from / 8) - (to / 8));
        if (dx <= 1 && dy <= 1) return true;
        return is_valid_castling(board_[from], from, to);
    }

    bool is_valid_castling(char piece, int from, int to) {
        if (tolower(piece) != 'k') return false;
        return true; // Placeholder: Implement castling logic
    }

    bool would_cause_check(int from, int to) {
        return false; // Placeholder: Implement check detection logic
    }
};

#endif
